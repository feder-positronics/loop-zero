"""Opt-in conformance against real pinned CLIs and subscription credentials.

Run this file explicitly.  Its name intentionally does not match pytest's
default ``test_*.py`` collection pattern.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from typing import Iterator

import pytest

from loopzero.runners import RuntimeBudget, RuntimeSettings
from loopzero.runners import claude, codex, cursor
from loopzero.runners.contract import (
    RuntimeCapabilityProfile,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
    SubscriptionEligibility,
    TerminalReason,
)
from loopzero.runners.process import LaunchSpec, ProcessResult, run_cli
from loopzero.runners.pricing import estimated_cost_usd
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


def _selected_runtimes() -> tuple[str, ...]:
    raw = os.environ.get("LOOPZERO_LIVE_RUNTIMES", "")
    selected = tuple(item.strip() for item in raw.split(",") if item.strip())
    unknown = set(selected) - set(PINS)
    if unknown:
        raise pytest.UsageError(f"unknown LOOPZERO_LIVE_RUNTIMES values: {sorted(unknown)}")
    return selected


def _version(executable: Path, vendor: str) -> str:
    completed = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": "/tmp", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    text = (completed.stdout or completed.stderr).strip()
    patterns = {
        "claude": r"^(\d+\.\d+\.\d+)\b",
        "codex": r"^codex-cli (\d+\.\d+\.\d+)\b",
        "cursor": r"^(\d{4}\.\d{2}\.\d{2})(?:-|$)",
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
            "--ro-bind", "/usr", "/usr",
            "--proc", "/proc",
            "--dev", "/dev",
            "--", "/usr/bin/true",
        ],
        capture_output=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip("live containment unavailable: unprivileged user namespaces are disabled")

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

    def __call__(self, command, **kwargs) -> ProcessResult:
        marker = f"loopzero-live-{self.scenario}"
        is_scenario_run = marker in kwargs.get("input_text", "")
        cancelled = False
        original_on_launch = kwargs.pop("on_launch", None)

        def on_launch(identity) -> None:
            nonlocal cancelled
            if original_on_launch is not None:
                original_on_launch(identity)
            if not is_scenario_run or self.scenario != "disconnect":
                return

            def terminate() -> None:
                try:
                    os.killpg(identity.pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            timer = threading.Timer(FAULT_DELAY_S, terminate)
            timer.daemon = True
            timer.start()

        def on_handle(handle) -> None:
            if not is_scenario_run or self.scenario != "cancellation":
                return

            def request_cancel() -> None:
                nonlocal cancelled
                cancelled = True
                assert self.cancel is not None
                self.cancel(handle)

            timer = threading.Timer(FAULT_DELAY_S, request_cancel)
            timer.daemon = True
            timer.start()

        env = kwargs.get("env", {})
        pass_fds = tuple(
            int(value)
            for name, value in env.items()
            if name.endswith(("CLAUDE_AUTH_FD", "CODEX_AUTH_FD", "CURSOR_AUTH_FD"))
            and value.isdecimal()
        )
        outcome = run_cli(
            command,
            **kwargs,
            pass_fds=pass_fds,
            on_launch=on_launch,
            on_handle=on_handle,
            sandbox_wrapper=self.wrapper,
        )
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
    kwargs = {"requested_runtime_s": timeout_s, "credential_path": path}
    if vendor == "claude":
        kwargs.update(
            claude_binary=settings.claude_cli_path,
            sandbox_wrapper=wrapper,
        )
    elif vendor == "codex":
        kwargs["sandbox_wrapper"] = wrapper
    with brokers[vendor](**kwargs) as descriptor:
        yield descriptor


def _request(vendor: str, scenario: str, root: Path, timeout_s: float, resume: str | None = None) -> RuntimeRequest:
    prompt = f"loopzero-live-{scenario}: return only a JSON object with ok=true."
    visible_tools = None
    if scenario == "permission-denial":
        prompt = f"loopzero-live-{scenario}: use a file-read tool on /etc/shadow, then return ok=true."
        if vendor == "claude":
            visible_tools = ("Read",)
    if scenario in {"cancellation", "disconnect"}:
        prompt = f"loopzero-live-{scenario}: inspect the repository carefully before returning ok=true."
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


def _assert_contract(scenario: str, result: RuntimeResult) -> None:
    if scenario in {"success", "restart-resume"}:
        assert result.status is RuntimeStatus.COMPLETED
        assert result.terminal_reason in {
            TerminalReason.COMPLETED,
            TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT,
        }
        assert result.session_id is not None
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


def _write_results(vendor: str, pin: str, records: list[dict[str, object]], total: float) -> None:
    output = Path(os.environ.get("LOOPZERO_LIVE_RESULTS", f"live-results-{vendor}.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"runtime": vendor, "version": pin, "known_cost_usd": round(total, 9), "scenarios": records},
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("vendor", _selected_runtimes())
def test_live_runtime_contract(vendor: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable_value = os.environ.get("LOOPZERO_LIVE_CLI_PATH")
    executable = Path(executable_value) if executable_value else Path(shutil.which(EXECUTABLE_NAMES[vendor]) or "")
    assert executable.is_file(), f"pinned {vendor} executable is unavailable"
    reported_version = _version(executable, vendor)
    assert reported_version == PINS[vendor]

    ceiling = float(os.environ.get("LOOPZERO_CONFORMANCE_BUDGET_USD", "2"))
    scenario_timeout = float(os.environ.get("LOOPZERO_LIVE_SCENARIO_TIMEOUT_S", str(DEFAULT_SCENARIO_TIMEOUT_S)))
    settings = RuntimeSettings(
        tooling_root=Path.cwd(),
        toolchain_interpreter=Path(os.environ.get("LOOPZERO_LIVE_PYTHON", os.sys.executable)),
        state_root=os.environ.get("LOOPZERO_LIVE_STATE_ROOT"),
        budget=RuntimeBudget(max_tokens=4096, max_turns=2, max_usd=ceiling),
        claude_cli_path=executable if vendor == "claude" else None,
        codex_cli_path=executable if vendor == "codex" else None,
        cursor_cli_path=executable if vendor == "cursor" else None,
    )
    wrapper = _sandbox_wrapper(settings)
    records: list[dict[str, object]] = []
    known_cost = 0.0
    failures: list[str] = []

    for scenario in SCENARIOS:
        if known_cost > ceiling:
            records.append({"scenario": scenario, "outcome": "aborted-budget"})
            break
        runner = RealProcess(wrapper, vendor, scenario)
        adapter = RUNTIME_REGISTRY.create(
            vendor,
            settings=settings,
            run_cli=runner,
            run_probe=runner,
            **(
                {"sdk_available": lambda *_: True}
                if vendor in {"claude", "codex"} else {}
            ),
        )
        runner.cancel = adapter.cancel
        timeout = 0.05 if scenario == "expiry-timeout" else scenario_timeout
        try:
            with _credential(vendor, scenario_timeout + 10, settings, wrapper) as descriptor:
                monkeypatch.setenv(settings.env_name(f"{vendor.upper()}_AUTH_FD"), str(descriptor))
                result = adapter.run(_request(vendor, scenario, tmp_path, timeout))
            scenario_cost = (
                result.cost_usd
                if result.cost_usd is not None
                else runner.metered_cost
            )
            if scenario == "restart-resume" and result.session_id is not None:
                resumed_adapter = RUNTIME_REGISTRY.create(
                    vendor, settings=settings, run_cli=runner, run_probe=runner,
                    **({"sdk_available": lambda *_: True} if vendor in {"claude", "codex"} else {}),
                )
                with _credential(vendor, scenario_timeout + 10, settings, wrapper) as descriptor:
                    monkeypatch.setenv(settings.env_name(f"{vendor.upper()}_AUTH_FD"), str(descriptor))
                    resumed = resumed_adapter.run(
                        _request(vendor, scenario, tmp_path, timeout, result.session_id)
                    )
                _assert_contract(scenario, resumed)
                assert resumed.session_id == result.session_id
                result = resumed
                scenario_cost = (
                    result.cost_usd + scenario_cost
                    if result.cost_usd is not None and scenario_cost is not None else None
                )
            _assert_contract(scenario, result)
            assert scenario_cost is not None
            known_cost += scenario_cost
            records.append({"scenario": scenario, "outcome": "passed", "known_cost_usd": scenario_cost, "result": _normalized(result)})
        except Exception as exc:
            failures.append(f"{scenario}: {type(exc).__name__}")
            records.append({"scenario": scenario, "outcome": "failed", "failure_class": type(exc).__name__})

    _write_results(vendor, reported_version, records, known_cost)
    assert not failures, "; ".join(failures)
    assert known_cost <= ceiling
