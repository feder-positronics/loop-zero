"""Opt-in conformance against real pinned CLIs and subscription credentials.

Run this file explicitly.  Its name intentionally does not match pytest's
default ``test_*.py`` collection pattern.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import math
from typing import Iterator

import pytest

from loopzero.runners import RuntimeBudget, RuntimeSettings
from loopzero.runners import claude, codex, cursor
from loopzero.runners.contract import (
    RuntimeCapabilityProfile,
    RuntimeAdapter,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
    SubscriptionEligibility,
    TerminalReason,
)
from loopzero.runners.process import LaunchSpec, ProcessResult, run_cli
from loopzero.runners.process import private_temporary_directory
from loopzero.runners.pricing import MODEL_PRICES, estimated_cost_usd
from loopzero.runners.registry import RUNTIME_REGISTRY


PINS = {
    "claude": claude.PINNED_CLAUDE_CLI_VERSION,
    "codex": codex.PINNED_CODEX_VERSION,
    "cursor": cursor.PINNED_CURSOR_CLI_VERSION,
}
EXECUTABLE_NAMES = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}
MODELS = {
    "claude": "claude-fable-5-1",
    "codex": "gpt-5.6-luna",
    "cursor": "cursor-grok-4.6-high",
}
SCENARIOS = (
    "success",
    "malformed-output",
    "permission-denial",
    "cancellation",
    "disconnect",
    "restart-resume",
    "expiry-timeout",
)
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
DEFAULT_SCENARIO_TIMEOUT_S = 45.0
FAULT_DELAY_S = 1.0
LIVE_MAX_OUTPUT_TOKENS = 32_768
DEFAULT_RUN_BUDGET_USD = 0.25
DEFAULT_CURSOR_KILLED_CHARGE_USD = 0.10
DEFAULT_MAX_KILLED_RUNS = 3
# Prompts are ASCII today, but one token per UTF-8 byte deliberately
# overestimates the normal tokenizer ratio for killed Codex requests.
CODEX_ESTIMATED_INPUT_TOKENS_PER_BYTE = 1.0
KILLED_SCENARIOS = frozenset({"cancellation", "disconnect", "expiry-timeout"})


def _selected_runtimes() -> tuple[str, ...]:
    raw = os.environ.get("LOOPZERO_LIVE_RUNTIMES", "")
    selected = tuple(item.strip() for item in raw.split(",") if item.strip())
    unknown = set(selected) - set(PINS)
    if unknown:
        raise pytest.UsageError(f"unknown LOOPZERO_LIVE_RUNTIMES values: {sorted(unknown)}")
    return selected


def _version(
    executable: Path,
    vendor: str,
    *,
    settings: RuntimeSettings,
    wrapper,
) -> str:
    with settings.use():
        completed = run_cli(
        [str(executable), "--version"],
        cwd=Path.cwd(),
        input_text="",
        timeout_s=10,
        env={"HOME": "/tmp", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        sandbox_wrapper=wrapper,
    )
    if completed.returncode != 0 or completed.timed_out or completed.output_limited:
        raise AssertionError(f"{vendor} version probe failed")
    text = (completed.stdout or completed.stderr).strip()
    patterns = {
        "claude": r"^(\d+\.\d+\.\d+)\b",
        "codex": r"^codex-cli (\d+\.\d+\.\d+)\b",
        "cursor": r"^(\d{4}\.\d{2}\.\d{2}-[0-9a-f]+)\b",
    }
    match = re.search(patterns[vendor], text)
    if match is None:
        raise AssertionError(f"{vendor} emitted an unrecognized version format")
    return match.group(1)


def _sandbox_wrapper(settings: RuntimeSettings):
    """Nest each adapter child in the checks.yml-style filesystem bubble."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("live containment unavailable: bwrap is not installed")
    probe = subprocess.run(
        [
            bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--die-with-parent",
            "--ro-bind", "/", "/",
            "--proc", "/proc",
            "--dev", "/dev",
            "--", "/usr/bin/true",
        ],
        capture_output=True,
        timeout=10,
    )
    if probe.returncode != 0:
        detail = probe.stderr.decode("utf-8", "replace").strip().splitlines()
        pytest.skip(
            "live containment unavailable: bubblewrap user-namespace probe failed: "
            + (detail[-1] if detail else f"exit {probe.returncode}")
        )

    roots = [Path("/usr"), Path("/etc"), Path("/bin"), Path("/lib"), Path("/lib64"), Path("/sbin")]
    tooling_root = (settings.tooling_root or Path.cwd()).resolve()
    interpreter = settings.interpreter(tooling_root).resolve()
    interpreter_root = interpreter.parent.parent
    workspace_root = settings.workspace_root(tooling_root)
    workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_root_value = os.environ.get("LOOPZERO_LIVE_RUNTIME_ROOT")
    runtime_root = Path(runtime_root_value) if runtime_root_value else None

    def wrap(spec: LaunchSpec) -> list[str]:
        command = [
            bwrap,
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--tmpfs", "/tmp", "--tmpfs", "/run", "--dev", "/dev", "--proc", "/proc",
        ]
        for root in roots:
            if root.is_symlink():
                command.extend(("--symlink", os.readlink(root), str(root)))
            elif root.exists():
                command.extend(("--ro-bind", str(root), str(root)))
        for parent in sorted(spec.private_tmpdir.parents, key=lambda item: len(item.parts)):
            if parent != Path("/"):
                command.extend(("--dir", str(parent)))
        command.extend(("--ro-bind", str(tooling_root), str(tooling_root)))
        if not interpreter.is_relative_to(tooling_root):
            command.extend(("--ro-bind", str(interpreter_root), str(interpreter_root)))
        if runtime_root is not None:
            command.extend(("--ro-bind", str(runtime_root), str(runtime_root)))
        command.extend(("--bind", str(workspace_root), str(workspace_root)))
        for parent in sorted(spec.cwd.parents, key=lambda item: len(item.parts)):
            if parent != Path("/"):
                command.extend(("--dir", str(parent)))
        if not spec.cwd.is_relative_to(workspace_root):
            command.extend(("--ro-bind", str(spec.cwd), str(spec.cwd)))
        for executable in (
            settings.claude_cli_path,
            settings.codex_cli_path,
            settings.cursor_cli_path,
        ):
            if executable is not None and not executable.is_relative_to(tooling_root):
                for parent in reversed(executable.parents[:-1]):
                    if parent != Path("/"):
                        command.extend(("--dir", str(parent)))
                command.extend(("--ro-bind", str(executable), str(executable)))
        command.extend(("--bind", str(spec.private_tmpdir), str(spec.private_tmpdir)))
        for mount in spec.private_mounts:
            command.extend(("--bind", str(mount), str(mount)))
        command.extend(("--chdir", str(spec.cwd), "--", *spec.argv))
        return command

    return wrap


class RealProcess:
    """Real contained runner with narrowly scoped fault controls."""

    def __init__(self, wrapper, vendor: str, scenario: str) -> None:
        self.wrapper = wrapper
        self.vendor = vendor
        self.scenario = scenario
        self.metered_cost: float | None = None
        self.cancel = None
        self._fault_timer: threading.Timer | None = None
        self.scenario_started = False
        self.killed = False

    def __call__(self, command, **kwargs) -> ProcessResult:
        marker = f"loopzero-live-{self.scenario}"
        is_scenario_run = marker in kwargs.get("input_text", "")
        self.scenario_started = self.scenario_started or is_scenario_run
        cancelled = False
        fault_injected = False
        original_on_launch = kwargs.pop("on_launch", None)

        def on_launch(identity) -> None:
            if original_on_launch is not None:
                original_on_launch(identity)

        def on_handle(handle) -> None:
            if not is_scenario_run or self.scenario not in {"cancellation", "disconnect"}:
                return

            def inject_fault() -> None:
                nonlocal cancelled, fault_injected
                cancelled = self.scenario == "cancellation"
                fault_injected = True
                assert self.cancel is not None
                self.cancel(handle)

            timer = threading.Timer(FAULT_DELAY_S, inject_fault)
            timer.daemon = True
            timer.start()
            self._fault_timer = timer

        env = kwargs.get("env", {})
        pass_fds = tuple(
            int(value)
            for name, value in env.items()
            if name.endswith(("CLAUDE_AUTH_FD", "CODEX_AUTH_FD", "CURSOR_AUTH_FD"))
            and value.isdecimal()
        )
        try:
            outcome = run_cli(
                command,
                **kwargs,
                pass_fds=pass_fds,
                on_launch=on_launch,
                on_handle=on_handle,
                sandbox_wrapper=self.wrapper,
            )
        finally:
            if self._fault_timer is not None:
                self._fault_timer.cancel()
        if is_scenario_run:
            try:
                parsed = {
                    "claude": claude.parse_claude_stream,
                    "codex": codex.parse_codex_stream,
                    "cursor": cursor.parse_cursor_stream,
                }[self.vendor](outcome.stdout)
                self.metered_cost = estimated_cost_usd(
                    MODELS[self.vendor], parsed.usage
                )
            except (ValueError, AttributeError):
                self.metered_cost = None
        if is_scenario_run and self.scenario == "malformed-output":
            outcome = replace(outcome, stdout=outcome.stdout + "\n{malformed-live-frame")
        if is_scenario_run:
            self.killed = self.killed or fault_injected or outcome.timed_out
        if cancelled:
            outcome = replace(outcome, cancelled=True)
        return outcome


@contextmanager
def _credential(vendor: str, timeout_s: float, settings: RuntimeSettings, wrapper) -> Iterator[int]:
    raw_path = os.environ.get("LOOPZERO_LIVE_CREDENTIAL_PATH")
    path = Path(raw_path) if raw_path else None
    brokers = {
        "claude": claude.claude_subscription_credential,
        "codex": codex.codex_subscription_credential,
        "cursor": cursor.cursor_subscription_credential,
    }
    if path is not None:
        metadata = path.stat(follow_symlinks=False)
        if (
            not path.is_file()
            or metadata.st_size <= 2
            or metadata.st_size > 1024 * 1024
        ):
            raise RuntimeError("live credential file is invalid")
    # A workflow-supplied file has already been validated/refreshed by the
    # trusted release wheel. It is access-only and safe to re-lend; without an
    # explicit file, local runs use the broker's normal host discovery.
    with settings.use():
        kwargs = {"requested_runtime_s": timeout_s}
        if path is not None:
            kwargs["credential_path"] = path
        if vendor == "claude":
            kwargs.update(
                claude_binary=settings.claude_cli_path,
                sandbox_wrapper=wrapper,
                allow_token_fallback=path is None,
            )
        elif vendor == "codex":
            kwargs["sandbox_wrapper"] = wrapper
        with brokers[vendor](**kwargs) as descriptor:
            yield descriptor


def _run_with_credential(
    vendor: str,
    adapter: RuntimeAdapter,
    request: RuntimeRequest,
    *,
    timeout_s: float,
    settings: RuntimeSettings,
    wrapper,
) -> RuntimeResult:
    """Run through the adapter while preserving the broker-owned descriptor."""
    descriptor_name = settings.env_name(f"{vendor.upper()}_AUTH_FD")
    previous = os.environ.get(descriptor_name)
    with _credential(vendor, timeout_s, settings, wrapper) as snapshot:
        # The process launch path consumes and closes credential descriptors.
        # Transfer a duplicate so the broker retains its own snapshot until its
        # context exits, including when adapter.run raises after launch.
        child_descriptor = os.dup(snapshot)
        os.environ[descriptor_name] = str(child_descriptor)
        try:
            return adapter.run(request)
        finally:
            if previous is None:
                os.environ.pop(descriptor_name, None)
            else:
                os.environ[descriptor_name] = previous
            try:
                os.close(child_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def _scenario_prompt(vendor: str, scenario: str) -> str:
    del vendor
    prompt = f"loopzero-live-{scenario}: return only a JSON object with ok=true."
    if scenario == "permission-denial":
        return (
            f"loopzero-live-{scenario}: use a file-read tool on /etc/shadow, "
            "then return ok=true."
        )
    if scenario in {"cancellation", "disconnect"}:
        return (
            f"loopzero-live-{scenario}: inspect the repository carefully before "
            "returning ok=true."
        )
    return prompt


def _request(vendor: str, scenario: str, root: Path, timeout_s: float, resume: str | None = None) -> RuntimeRequest:
    prompt = _scenario_prompt(vendor, scenario)
    visible_tools = None
    if scenario == "permission-denial":
        if vendor == "claude":
            visible_tools = ("Read",)
    return RuntimeRequest(
        vendor=vendor,
        transport=RUNTIME_REGISTRY.registration(vendor).preferred_transport,
        requested_model=MODELS[vendor],
        effort="low",
        prompt=prompt,
        cwd=root,
        timeout_s=timeout_s,
        read_only=True,
        attempt_id=f"live-{vendor}-{scenario}",
        tooling_root=Path.cwd(),
        eligibility=SubscriptionEligibility.APPROVED,
        budget_usd=None,
        output_schema=SCHEMA,
        capability_profile=RuntimeCapabilityProfile(read_roots=(root,)),
        visible_tools=visible_tools,
        allowed_tools=(),
        resume_session_id=resume,
    )


def _normalized(result: RuntimeResult) -> dict[str, object]:
    usage = asdict(result.usage) if result.usage is not None else None
    return {
        "vendor": result.vendor,
        "transport": result.transport,
        "status": result.status.value,
        "terminal_reason": result.terminal_reason.value,
        "session_id_present": result.session_id is not None,
        "usage": usage,
        "cost_usd": result.cost_usd,
        "cost_status": result.cost_status.value,
        "returncode": result.returncode,
        "duration_s": result.duration_s,
    }


def _cursor_unsupported_reason(vendor: str, scenario: str) -> str | None:
    if vendor == "cursor" and scenario in {"success", "restart-resume"}:
        return "pinned Cursor CLI cannot enforce output_schema"
    return None


def _suite_stop_reason(
    *, charged_cost: float, ceiling: float, killed_runs: int, max_killed_runs: int
) -> str | None:
    if charged_cost >= ceiling:
        return "aborted-budget"
    if killed_runs >= max_killed_runs:
        return "aborted-killed-limit"
    return None


def _permission_denial_evident(result: RuntimeResult) -> bool:
    diagnostics = tuple(item.casefold() for item in result.diagnostics)
    explicit_counter = any(
        item.startswith((
            "claude permission denials:",
            "claude denied permission checks:",
        ))
        for item in diagnostics
    )
    explicit_event = any(
        event.kind.casefold() == "permission"
        and (event.subtype or "").casefold() in {"denied", "permission-denied"}
        for event in result.events
    )
    path_named_tool_denial = any(
        event.kind.casefold() in {"tool", "tool-denied", "file"}
        and "denied" in (event.subtype or "").casefold()
        and "/etc/shadow" in (event.subtype or "").casefold()
        for event in result.events
    )
    return explicit_counter or explicit_event or path_named_tool_denial


def _assert_contract(scenario: str, result: RuntimeResult) -> None:
    if scenario in {"success", "restart-resume"}:
        assert result.status is RuntimeStatus.COMPLETED
        assert result.terminal_reason in {
            TerminalReason.COMPLETED,
            TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT,
        }
        assert result.session_id is not None
        assert result.vendor != "cursor"
        assert result.structured_output == {"ok": True}
    elif scenario == "malformed-output":
        assert result.status is RuntimeStatus.FAILED
        assert result.terminal_reason in {
            TerminalReason.MALFORMED_EVENT,
            TerminalReason.PROTOCOL_FAILURE,
        }
    elif scenario == "permission-denial":
        assert result.status is RuntimeStatus.FAILED
        assert result.terminal_reason in {
            TerminalReason.PROCESS_EXIT,
            TerminalReason.MODEL_RESULT,
        }
        assert _permission_denial_evident(result)
    elif scenario == "cancellation":
        assert result.status is RuntimeStatus.CANCELLED
        assert result.terminal_reason is TerminalReason.CANCELLED
    elif scenario == "disconnect":
        assert result.status is RuntimeStatus.FAILED
        assert result.terminal_reason in {
            TerminalReason.PROCESS_EXIT,
            TerminalReason.TRANSPORT_DISCONNECT,
        }
    else:
        assert result.status is RuntimeStatus.TIMED_OUT
        assert result.terminal_reason is TerminalReason.TIMEOUT
    assert result.vendor in {"claude", "codex", "cursor"}


def _write_results(
    vendor: str,
    pin: str,
    records: list[dict[str, object]],
    total: float,
    *,
    killed_runs: int,
    max_killed_runs: int,
) -> None:
    output = Path(os.environ.get("LOOPZERO_LIVE_RESULTS", f"live-results-{vendor}.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "runtime": vendor,
                "version": pin,
                "charged_cost_usd": round(total, 9),
                "killed_runs": killed_runs,
                "max_killed_runs": max_killed_runs,
                "spend_bound": (
                    "fixed killed-run charge plus aggregate charged-cost ceiling"
                    if vendor == "cursor"
                    else "vendor cap plus aggregate charged-cost ceiling"
                ),
                "scenarios": records,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def _vendor_cap_charge(
    vendor: str,
    budget: RuntimeBudget,
    *,
    prompt_bytes: int = 0,
) -> float:
    """Return the conservative maximum charge for one killed invocation."""
    if vendor == "claude":
        return budget.max_usd
    if vendor == "codex":
        price = MODEL_PRICES[MODELS[vendor]]
        estimated_input_tokens = math.ceil(
            prompt_bytes * CODEX_ESTIMATED_INPUT_TOKENS_PER_BYTE
        )
        return round(
            (
                estimated_input_tokens * price.input_per_million
                + budget.max_tokens * price.output_per_million
            )
            / 1_000_000,
            9,
        )
    return DEFAULT_CURSOR_KILLED_CHARGE_USD


def _accounted_cost(
    vendor: str,
    scenario: str,
    observed: float | None,
    budget: RuntimeBudget,
    *,
    prompt_bytes: int = 0,
) -> tuple[float | None, str]:
    if scenario in KILLED_SCENARIOS:
        conservative = _vendor_cap_charge(
            vendor, budget, prompt_bytes=prompt_bytes
        )
        charge = max(conservative, observed or 0.0)
        return charge, (
            "fixed-conservative-killed-charge"
            if vendor == "cursor"
            else "conservative-vendor-cap"
        )
    if observed is not None:
        return observed, "known"
    return None, "unknown"


def _remaining_budget(
    vendor: str,
    remaining_usd: float,
    *,
    prompt_bytes: int = 0,
) -> RuntimeBudget:
    max_tokens = LIVE_MAX_OUTPUT_TOKENS
    if vendor in {"codex", "cursor"}:
        output_rate = MODEL_PRICES[MODELS[vendor]].output_per_million
        input_reserve = 0.0
        if vendor == "codex":
            input_reserve = (
                math.ceil(prompt_bytes * CODEX_ESTIMATED_INPUT_TOKENS_PER_BYTE)
                * MODEL_PRICES[MODELS[vendor]].input_per_million
                / 1_000_000
            )
        output_allowance = max(0.0, remaining_usd - input_reserve)
        max_tokens = min(
            max_tokens,
            max(1, int(output_allowance * 1_000_000 / output_rate)),
        )
    return RuntimeBudget(
        max_tokens=max_tokens,
        max_turns=2,
        max_usd=min(DEFAULT_RUN_BUDGET_USD, remaining_usd),
    )


@pytest.mark.parametrize("vendor", _selected_runtimes())
def test_live_runtime_contract(vendor: str, tmp_path: Path) -> None:
    executable_value = os.environ.get("LOOPZERO_LIVE_CLI_PATH")
    executable = Path(executable_value) if executable_value else Path(shutil.which(EXECUTABLE_NAMES[vendor]) or "")
    assert executable.is_file(), f"pinned {vendor} executable is unavailable"

    ceiling = float(os.environ.get("LOOPZERO_CONFORMANCE_BUDGET_USD", "2"))
    max_killed_runs = int(os.environ.get(
        "LOOPZERO_CONFORMANCE_MAX_KILLED_RUNS", str(DEFAULT_MAX_KILLED_RUNS)
    ))
    if max_killed_runs <= 0:
        raise pytest.UsageError("LOOPZERO_CONFORMANCE_MAX_KILLED_RUNS must be positive")
    scenario_timeout = float(os.environ.get("LOOPZERO_LIVE_SCENARIO_TIMEOUT_S", str(DEFAULT_SCENARIO_TIMEOUT_S)))
    budget = _remaining_budget(vendor, ceiling)
    base_settings = RuntimeSettings(
        tooling_root=Path.cwd(),
        toolchain_interpreter=Path(os.environ.get("LOOPZERO_LIVE_PYTHON", os.sys.executable)),
        state_root=os.environ.get("LOOPZERO_LIVE_STATE_ROOT"),
        budget=budget,
        claude_cli_path=executable if vendor == "claude" else None,
        codex_cli_path=executable if vendor == "codex" else None,
        cursor_cli_path=executable if vendor == "cursor" else None,
    )
    records: list[dict[str, object]] = []
    charged_cost = 0.0
    killed_runs = 0
    failures: list[str] = []
    with base_settings.use(), private_temporary_directory(
        f"{vendor}-suite-session"
    ) as session_home:
        settings = replace(base_settings, session_home=session_home)
        wrapper = _sandbox_wrapper(settings)
        reported_version = _version(
            executable, vendor, settings=settings, wrapper=wrapper
        )
        assert reported_version == PINS[vendor]

        for scenario in SCENARIOS:
            stop_reason = _suite_stop_reason(
                charged_cost=charged_cost,
                ceiling=ceiling,
                killed_runs=killed_runs,
                max_killed_runs=max_killed_runs,
            )
            if stop_reason is not None:
                record: dict[str, object] = {
                    "scenario": scenario,
                    "outcome": stop_reason,
                }
                if stop_reason == "aborted-killed-limit":
                    record["killed_runs"] = killed_runs
                records.append(record)
                break
            unsupported_reason = _cursor_unsupported_reason(vendor, scenario)
            if unsupported_reason is not None:
                records.append({
                    "scenario": scenario,
                    "outcome": "unsupported",
                    "status": "unsupported",
                    "reason": unsupported_reason,
                    "accounting": "not-launched",
                    "charged_cost_usd": 0.0,
                })
                continue
            invocation_count = 2 if scenario == "restart-resume" else 1
            timeout = 0.05 if scenario == "expiry-timeout" else scenario_timeout
            prompt_bytes = len(_scenario_prompt(vendor, scenario).encode("utf-8"))
            scenario_budget = _remaining_budget(
                vendor,
                (ceiling - charged_cost) / invocation_count,
                prompt_bytes=prompt_bytes,
            )
            if (
                scenario in KILLED_SCENARIOS
                and _vendor_cap_charge(
                    vendor, scenario_budget, prompt_bytes=prompt_bytes
                ) > ceiling - charged_cost
            ):
                records.append({"scenario": scenario, "outcome": "aborted-budget"})
                break
            scenario_settings = replace(settings, budget=scenario_budget)
            runner = RealProcess(wrapper, vendor, scenario)
            adapter = RUNTIME_REGISTRY.create(
                vendor,
                settings=scenario_settings,
                run_cli=runner,
                run_probe=runner,
                **(
                    {"sdk_available": lambda *_: True}
                    if vendor in {"claude", "codex"} else {}
                ),
            )
            runner.cancel = adapter.cancel
            scenario_charge: float | None = None
            accounting = "unknown"
            observed_cost: float | None = None
            invocation_costs: list[float | None] = []
            try:
                invocation_costs.append(None)
                result = _run_with_credential(
                    vendor,
                    adapter,
                    _request(vendor, scenario, tmp_path, timeout),
                    timeout_s=scenario_timeout + 10,
                    settings=scenario_settings,
                    wrapper=wrapper,
                )
                observed_cost = (
                    result.cost_usd
                    if result.cost_usd is not None
                    else runner.metered_cost
                )
                invocation_costs[-1] = observed_cost
                if scenario == "restart-resume" and result.session_id is not None:
                    resumed_adapter = RUNTIME_REGISTRY.create(
                        vendor, settings=scenario_settings, run_cli=runner, run_probe=runner,
                        **({"sdk_available": lambda *_: True} if vendor in {"claude", "codex"} else {}),
                    )
                    invocation_costs.append(None)
                    resumed = _run_with_credential(
                        vendor,
                        resumed_adapter,
                        _request(
                            vendor,
                            scenario,
                            tmp_path,
                            timeout,
                            result.session_id,
                        ),
                        timeout_s=scenario_timeout + 10,
                        settings=scenario_settings,
                        wrapper=wrapper,
                    )
                    resumed_cost = resumed.cost_usd if resumed.cost_usd is not None else runner.metered_cost
                    invocation_costs[-1] = resumed_cost
                    original_session_id = result.session_id
                    result = resumed
                    observed_cost = (
                        observed_cost + resumed_cost
                        if observed_cost is not None and resumed_cost is not None
                        else None
                    )
                scenario_charge, accounting = _accounted_cost(
                    vendor,
                    scenario,
                    observed_cost,
                    scenario_budget,
                    prompt_bytes=prompt_bytes,
                )
                assert scenario_charge is not None
                charged_cost += scenario_charge
                _assert_contract(scenario, result)
                if scenario == "restart-resume":
                    assert result.session_id == original_session_id
                record: dict[str, object] = {
                    "scenario": scenario,
                    "outcome": "passed",
                    "accounting": accounting,
                    "charged_cost_usd": scenario_charge,
                    "result": _normalized(result),
                }
                if observed_cost is not None:
                    record["known_cost_usd"] = observed_cost
                records.append(record)
            except Exception as exc:
                if scenario_charge is None and runner.scenario_started:
                    fallback_charge = (
                        _vendor_cap_charge(
                            vendor, scenario_budget, prompt_bytes=prompt_bytes
                        )
                        if scenario in KILLED_SCENARIOS
                        else None
                    )
                    scenario_charge = sum(
                        value if value is not None else (fallback_charge or 0.0)
                        for value in invocation_costs
                    )
                    if all(value is not None for value in invocation_costs):
                        accounting = "known"
                    elif fallback_charge is not None and vendor != "cursor":
                        accounting = "conservative-vendor-cap"
                    elif fallback_charge is not None:
                        accounting = "fixed-conservative-killed-charge"
                    charged_cost += scenario_charge
                failures.append(f"{scenario}: {type(exc).__name__}")
                records.append({
                    "scenario": scenario,
                    "outcome": "failed",
                    "failure_class": type(exc).__name__,
                    "accounting": accounting,
                    "charged_cost_usd": scenario_charge,
                })
            finally:
                if runner.killed:
                    killed_runs += 1

    _write_results(
        vendor,
        reported_version,
        records,
        charged_cost,
        killed_runs=killed_runs,
        max_killed_runs=max_killed_runs,
    )
    assert not failures, "; ".join(failures)
    assert charged_cost <= ceiling
