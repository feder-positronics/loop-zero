"""Opt-in conformance against real pinned CLIs and subscription credentials.

Run this file explicitly.  Its name intentionally does not match pytest's
default ``test_*.py`` collection pattern.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, replace
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import math
from typing import Iterator

import pytest

from loopzero import credential_seal
from loopzero.runners import RuntimeBudget, RuntimeSettings
from loopzero.runners import claude, codex, cursor
from loopzero.runners.contract import (
    RuntimeCapabilityProfile,
    RuntimeAdapter,
    RuntimeBillingMode,
    RuntimeCostSource,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
    SubscriptionEligibility,
    TerminalReason,
)
from loopzero.runners.process import LaunchSpec, ProcessResult, run_cli
from loopzero.runners.process import private_temporary_directory
from loopzero.runners.pricing import MODEL_PRICES, resolved_cost_usd
from loopzero.runners.registry import RUNTIME_REGISTRY
from loopzero.runners.settings import get_settings


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
DEFAULT_MAX_UNACCOUNTED_RUNS = 3
HOST_SEAL_TIMEOUT_S = 60
# Prompts are ASCII today, but one token per UTF-8 byte deliberately
# overestimates the normal tokenizer ratio for killed Codex requests.
CODEX_ESTIMATED_INPUT_TOKENS_PER_BYTE = 1.0
KILLED_SCENARIOS = frozenset({"cancellation", "disconnect", "expiry-timeout"})


def _interpreter_symlink_chain(interpreter: Path) -> tuple[tuple[Path, str], ...]:
    """Return every lexical symlink traversed while resolving an interpreter."""
    remaining = deque(Path(os.path.abspath(interpreter)).parts[1:])
    current = Path("/")
    links: list[tuple[Path, str]] = []
    while remaining:
        part = remaining.popleft()
        current = current.parent if part == ".." else current / part
        if not current.is_symlink():
            continue
        if len(links) >= 40:
            raise RuntimeError("live interpreter has too many symlink hops")
        target = os.readlink(current)
        links.append((current, target))
        target_path = Path(target)
        current = Path("/") if target_path.is_absolute() else current.parent
        parts = target_path.parts[1:] if target_path.is_absolute() else target_path.parts
        remaining.extendleft(reversed(parts))
    return tuple(links)


def _name_resolution_binds(
    etc: Path = Path("/etc"), run: Path = Path("/run")
) -> tuple[Path, ...]:
    """Return resolver files that live outside the read-only system roots.

    Live provider calls keep the host network, but the bubble mounts a fresh
    tmpfs over ``/run``.  systemd-resolved hosts symlink ``/etc/resolv.conf``
    into ``/run``, so without binding the resolved target every provider
    request fails name resolution and both CLIs retry until the timeout.
    """
    binds: list[Path] = []
    for name in ("resolv.conf",):
        link = etc / name
        if not link.is_symlink():
            continue
        try:
            target = link.resolve(strict=True)
        except OSError:
            continue
        if target.is_file() and target.is_relative_to(run):
            binds.append(target)
    return tuple(binds)


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
    """Nest each adapter child in the live filesystem-only bubble.

    Live provider calls require the host network.  This wrapper deliberately
    omits ``--unshare-net`` while retaining the positive filesystem allowlist,
    private process namespace, dropped capabilities, and mandatory wrapper.
    """
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
    interpreter = settings.interpreter(tooling_root)
    venv_root = interpreter.parent.parent
    resolved_interpreter = Path(os.path.realpath(interpreter))
    interpreter_root = resolved_interpreter.parent.parent
    interpreter_links = _interpreter_symlink_chain(interpreter)
    bound_interpreter_roots = (*roots, tooling_root, venv_root, interpreter_root)
    modeled_links = tuple(
        (link, target)
        for link, target in interpreter_links
        if not any(
            link == root or link.is_relative_to(root)
            for root in bound_interpreter_roots
        )
    )
    for link, target in modeled_links:
        target_path = Path(target)
        lexical_target = (
            target_path if target_path.is_absolute() else link.parent / target_path
        )
        try:
            resolved_target = lexical_target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("live interpreter symlink target is unavailable") from exc
        if not any(
            resolved_target == root or resolved_target.is_relative_to(root)
            for root in bound_interpreter_roots
        ):
            raise RuntimeError("live interpreter symlink escapes read-only roots")
    workspace_root = settings.workspace_root(tooling_root)
    workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    name_resolution_binds = _name_resolution_binds()
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
        for target in name_resolution_binds:
            command.extend(("--ro-bind", str(target), str(target)))
        for parent in sorted(spec.private_tmpdir.parents, key=lambda item: len(item.parts)):
            if parent != Path("/"):
                command.extend(("--dir", str(parent)))
        command.extend(("--ro-bind", str(tooling_root), str(tooling_root)))
        if venv_root != tooling_root:
            command.extend(("--ro-bind", str(venv_root), str(venv_root)))
        if not resolved_interpreter.is_relative_to(venv_root):
            layout_paths = (interpreter_root, *(link for link, _target in modeled_links))
            layout_parents = {
                parent
                for path in layout_paths
                for parent in path.parents
                if parent != Path("/")
            }
            for parent in sorted(layout_parents, key=lambda item: len(item.parts)):
                command.extend(("--dir", str(parent)))
            for link, target in modeled_links:
                command.extend(("--symlink", target, str(link)))
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
        self.metered_cost_source = RuntimeCostSource.UNKNOWN
        self.cancel = None
        self._fault_timer: threading.Timer | None = None
        self.scenario_started = False
        self.scenario_invocations = 0
        self.killed = False

    def __call__(self, command, **kwargs) -> ProcessResult:
        marker = f"loopzero-live-{self.scenario}"
        is_scenario_run = marker in kwargs.get("input_text", "")
        self.scenario_started = self.scenario_started or is_scenario_run
        if is_scenario_run:
            self.scenario_invocations += 1
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
        # Credential descriptors are single-use launch authority. Inherit one
        # only into the SDK bridge that consumes it, never into version or
        # bootstrap probes. In particular, run_cli closes an explicitly lent
        # Codex descriptor after launch.
        is_sdk_bridge = (
            len(command) >= 3
            and command[1] == "-I"
            and Path(command[2]).name == get_settings().bridge_path.name
        )
        pass_fds = (
            tuple(
                int(value)
                for name, value in env.items()
                if name.endswith(("CLAUDE_AUTH_FD", "CODEX_AUTH_FD"))
                and value.isdecimal()
            )
            if is_sdk_bridge
            else ()
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
                self.metered_cost, self.metered_cost_source = resolved_cost_usd(
                    MODELS[self.vendor],
                    parsed.usage,
                    getattr(parsed, "cost_usd", None),
                )
            except (ValueError, AttributeError):
                self.metered_cost = None
                self.metered_cost_source = RuntimeCostSource.UNKNOWN
        if is_scenario_run and self.scenario == "malformed-output":
            outcome = replace(outcome, stdout=outcome.stdout + "\n{malformed-live-frame")
        if is_scenario_run:
            self.killed = self.killed or fault_injected or outcome.timed_out
        if cancelled:
            outcome = replace(outcome, cancelled=True)
        return outcome


def _validate_access_only_path(vendor: str, path: Path) -> Path:
    """Validate the suite input before any readiness or scenario process."""

    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("live access-only credential path is unsafe")
    try:
        metadata = path.stat(follow_symlinks=False)
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("live access-only credential path is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or metadata.st_size <= 2
        or metadata.st_size > credential_seal.MAX_SNAPSHOT_BYTES
        or len(payload) != metadata.st_size
    ):
        raise RuntimeError("live access-only credential path is unsafe")
    credential_seal._validate_access_only(vendor, payload)
    return path


def _seal_command() -> Path:
    configured = os.environ.get("LOOPZERO_LIVE_CREDENTIAL_SEAL")
    if configured:
        command = Path(configured)
    else:
        discovered = shutil.which("loopzero-credential-seal")
        if discovered is None:
            raise RuntimeError("live credential sealing command is unavailable")
        command = Path(discovered)
    if (
        not command.is_absolute()
        or not command.is_file()
        or not os.access(command, os.X_OK)
    ):
        raise RuntimeError("live credential sealing command is unavailable")
    return command


@contextmanager
def _suite_access_only_credential(vendor: str, root: Path) -> Iterator[Path]:
    """Seal once on the host, then expose only that snapshot to scenarios."""

    existing = os.environ.get("LOOPZERO_LIVE_CREDENTIAL_PATH")
    created = existing is None
    path = Path(existing) if existing is not None else root / "access-only.json"
    previous = existing
    if created:
        command = [str(_seal_command()), vendor, "--out", str(path)]
        source = os.environ.get("LOOPZERO_LIVE_CREDENTIAL_SOURCE")
        if source is not None:
            command.extend(("--source", source))
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=HOST_SEAL_TIMEOUT_S,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("live credential sealing failed")
    path = _validate_access_only_path(vendor, path)
    os.environ["LOOPZERO_LIVE_CREDENTIAL_PATH"] = str(path)
    try:
        yield path
    finally:
        if previous is None:
            os.environ.pop("LOOPZERO_LIVE_CREDENTIAL_PATH", None)
        else:
            os.environ["LOOPZERO_LIVE_CREDENTIAL_PATH"] = previous
        if created:
            path.unlink(missing_ok=True)


@contextmanager
def _credential(vendor: str, timeout_s: float, settings: RuntimeSettings, wrapper) -> Iterator[int]:
    del wrapper
    raw_path = os.environ.get("LOOPZERO_LIVE_CREDENTIAL_PATH")
    if not raw_path:
        raise RuntimeError("live suite preflight did not provide a sealed credential")
    path = _validate_access_only_path(vendor, Path(raw_path))
    brokers = {
        "claude": claude.claude_subscription_credential,
        "codex": codex.codex_subscription_credential,
        "cursor": cursor.cursor_subscription_credential,
    }
    # Host preflight has already validated/refreshed this source exactly once.
    # Re-lending the access-only snapshot cannot enter a refresh path, so no
    # scenario wrapper is passed to the broker at all.
    with settings.use():
        kwargs = {"requested_runtime_s": timeout_s, "credential_path": path}
        if vendor == "claude":
            kwargs.update(
                allow_token_fallback=False,
            )
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
    """Attach a local billing observation from this run's brokered credential.

    All current suite brokers validate subscription/browser-login credentials;
    there is no metered broker. This is conformance evidence, not signed review
    authority or an assertion that inference ran or incurred a payment.
    """
    descriptor_name = settings.env_name(f"{vendor.upper()}_AUTH_FD")
    previous = os.environ.get(descriptor_name)
    with _credential(vendor, timeout_s, settings, wrapper) as snapshot:
        # Inspect the exact held snapshot, not its original path, an environment
        # label, or a mode supplied by the adapter/vendor. The private broker
        # context above establishes its provenance; schema alone does not.
        credential_seal._validate_access_only(
            vendor, credential_seal._read_snapshot(snapshot)
        )
        # The process launch path consumes and closes credential descriptors.
        # Transfer a duplicate so the broker retains its own snapshot until its
        # context exits, including when adapter.run raises after launch.
        child_descriptor = os.dup(snapshot)
        os.environ[descriptor_name] = str(child_descriptor)
        try:
            result = adapter.run(request)
            preferred = RUNTIME_REGISTRY.registration(vendor).preferred_transport
            same_route = (
                request.vendor == result.vendor == vendor
                and request.attempt_id == result.attempt_id
                and request.transport == result.transport == preferred
                and all(
                    attempt.transport == preferred
                    for attempt in result.transport_attempts
                )
            )
            return replace(
                result,
                billing_mode=(
                    RuntimeBillingMode.SUBSCRIPTION
                    if same_route
                    else RuntimeBillingMode.UNKNOWN
                ),
            )
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


def _probe_with_credential(
    vendor: str,
    adapter: RuntimeAdapter,
    request: RuntimeRequest,
    *,
    timeout_s: float,
    settings: RuntimeSettings,
    wrapper,
):
    """Run the no-turn readiness probe with one disposable descriptor."""

    descriptor_name = settings.env_name(f"{vendor.upper()}_AUTH_FD")
    previous = os.environ.get(descriptor_name)
    with _credential(vendor, timeout_s, settings, wrapper) as snapshot:
        child_descriptor = os.dup(snapshot)
        os.environ[descriptor_name] = str(child_descriptor)
        try:
            return adapter.probe(request)
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
    prompt = f"loopzero-live-{scenario}: return only a JSON object with ok=true."
    if scenario == "permission-denial":
        if vendor == "codex":
            return (
                f"loopzero-live-{scenario}: run exactly /usr/bin/cat -- /etc/shadow "
                "using the command tool, then return ok=true."
            )
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
    """Retain cost_usd as the API-equivalent usage proxy, not a payment record."""
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
        "cost_source": result.cost_source.value,
        "billing_mode": result.billing_mode.value,
        "returncode": result.returncode,
        "duration_s": result.duration_s,
        "diagnostics": list(result.diagnostics),
        "transport_attempts": [
            asdict(attempt) for attempt in result.transport_attempts
        ],
    }


def _normalized_readiness(readiness) -> dict[str, object]:
    return {
        "ready": readiness.ready,
        "transport": readiness.transport,
        "eligibility": readiness.eligibility.value,
        "failure": readiness.failure.value if readiness.failure else None,
        "diagnostic": readiness.repair,
    }


def _print_readiness(vendor: str, version: str, readiness) -> None:
    """Print only bounded version and readiness fields, never provider output."""

    failure = readiness.failure.value if readiness.failure else "-"
    diagnostic = readiness.repair or "ready"
    print("| runtime | seal | version | transport | ready | failure | diagnostic |")
    print("|---|---|---|---|---|---|---|")
    print(
        f"| {vendor} | access-only | {version} | "
        f"{readiness.transport or '-'} | {'yes' if readiness.ready else 'no'} | "
        f"{failure} | {diagnostic} |"
    )


def _unsupported_reason(vendor: str, scenario: str) -> str | None:
    """Return the vendor limitation that makes a scenario unobservable."""
    if vendor == "cursor" and scenario in {"success", "restart-resume"}:
        return "pinned Cursor CLI cannot enforce output_schema"
    return None


_cursor_unsupported_reason = _unsupported_reason


def _suite_stop_reason(
    *,
    charged_cost: float,
    ceiling: float,
    killed_runs: int,
    max_killed_runs: int,
    unaccounted_runs: int,
    max_unaccounted_runs: int,
) -> str | None:
    if charged_cost >= ceiling:
        return "aborted-budget"
    if killed_runs >= max_killed_runs:
        return "aborted-killed-limit"
    if unaccounted_runs >= max_unaccounted_runs:
        return "aborted-unaccounted-limit"
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


def _post_launch_unsupported_reason(scenario: str, result: RuntimeResult) -> str | None:
    """Allow the named Codex exception only after an otherwise valid turn."""
    if (
        result.vendor == "codex"
        and scenario == "permission-denial"
        and not _permission_denial_evident(result)
        and (
            (result.status is RuntimeStatus.COMPLETED
             and result.terminal_reason is TerminalReason.COMPLETED)
            or (result.status is RuntimeStatus.FAILED
                and result.terminal_reason in {TerminalReason.PROCESS_EXIT, TerminalReason.MODEL_RESULT})
        )
    ):
        return (
            "pinned Codex permission-denial turn produced no observable denial "
            "for the attempted read; command outcomes are recorded separately"
        )
    return None


def _observed_tool_outcomes(result: RuntimeResult) -> list[str]:
    """Use the adapter's bounded metadata; never upload raw tool output."""
    return [
        item for item in result.diagnostics
        if item.startswith("Codex command completed:")
    ] or ["no command completion observed"]


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
        # The governed denial is the contract.  A runtime may either fail the
        # turn or, like Claude, report the denial and let the model finish.
        assert result.status in {RuntimeStatus.FAILED, RuntimeStatus.COMPLETED}
        if result.status is RuntimeStatus.FAILED:
            assert result.terminal_reason in {
                TerminalReason.PROCESS_EXIT,
                TerminalReason.MODEL_RESULT,
            }
        else:
            assert result.terminal_reason is TerminalReason.COMPLETED
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
    unaccounted_runs: int,
    max_unaccounted_runs: int,
    readiness: dict[str, object] | None = None,
) -> None:
    output = Path(os.environ.get("LOOPZERO_LIVE_RESULTS", f"live-results-{vendor}.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "runtime": vendor,
                "version": pin,
                # Budget debit includes conservative charges for unknown usage.
                "charged_cost_usd": round(total, 9),
                "killed_runs": killed_runs,
                "max_killed_runs": max_killed_runs,
                "unaccounted_runs": unaccounted_runs,
                "max_unaccounted_runs": max_unaccounted_runs,
                "spend_bound": (
                    "fixed conservative budget debit plus aggregate API-equivalent USD ceiling"
                    if vendor == "cursor"
                    else "vendor cap plus aggregate API-equivalent USD ceiling"
                ),
                "readiness": readiness,
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
    """Return the conservative fallback charge for one invocation."""
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
    cost_source: RuntimeCostSource = RuntimeCostSource.UNKNOWN,
    prompt_bytes: int = 0,
) -> tuple[float | None, str]:
    conservative = _vendor_cap_charge(
        vendor, budget, prompt_bytes=prompt_bytes
    )
    known_cost = (
        observed
        if cost_source in {RuntimeCostSource.VENDOR, RuntimeCostSource.ESTIMATED}
        else None
    )
    if scenario in KILLED_SCENARIOS:
        charge = max(conservative, known_cost or 0.0)
        return charge, (
            "fixed-conservative-killed-charge"
            if vendor == "cursor"
            else "conservative-vendor-cap"
        )
    if known_cost is not None:
        return known_cost, "known"
    return conservative, (
        "fixed-conservative-charge"
        if vendor == "cursor"
        else "conservative-vendor-cap"
    )


def _account_invocations(
    vendor: str,
    scenario: str,
    observed_costs: list[float | None],
    budget: RuntimeBudget,
    *,
    cost_sources: list[RuntimeCostSource] | None = None,
    prompt_bytes: int = 0,
) -> tuple[float, str, int]:
    sources = (
        cost_sources
        if cost_sources is not None
        else [RuntimeCostSource.UNKNOWN] * len(observed_costs)
    )
    if len(sources) != len(observed_costs):
        raise ValueError("cost source count must match invocation count")
    accounted = [
        _accounted_cost(
            vendor,
            scenario,
            observed,
            budget,
            cost_source=source,
            prompt_bytes=prompt_bytes,
        )
        for observed, source in zip(observed_costs, sources, strict=True)
    ]
    unaccounted = sum(
        observed is None
        or source not in {RuntimeCostSource.VENDOR, RuntimeCostSource.ESTIMATED}
        for observed, source in zip(observed_costs, sources, strict=True)
    )
    charge = sum(item[0] for item in accounted if item[0] is not None)
    if all(item[1] == "known" for item in accounted):
        accounting = "known"
    elif vendor == "cursor":
        accounting = (
            "fixed-conservative-killed-charge"
            if scenario in KILLED_SCENARIOS
            else "fixed-conservative-charge"
        )
    else:
        accounting = "conservative-vendor-cap"
    return charge, accounting, unaccounted


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
def test_live_runtime_contract(
    vendor: str, tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    executable_value = os.environ.get("LOOPZERO_LIVE_CLI_PATH")
    executable = Path(executable_value) if executable_value else Path(shutil.which(EXECUTABLE_NAMES[vendor]) or "")
    assert executable.is_file(), f"pinned {vendor} executable is unavailable"

    ceiling = float(os.environ.get("LOOPZERO_CONFORMANCE_BUDGET_USD", "2"))
    max_killed_runs = int(os.environ.get(
        "LOOPZERO_CONFORMANCE_MAX_KILLED_RUNS", str(DEFAULT_MAX_KILLED_RUNS)
    ))
    if max_killed_runs <= 0:
        raise pytest.UsageError("LOOPZERO_CONFORMANCE_MAX_KILLED_RUNS must be positive")
    max_unaccounted_runs = int(os.environ.get(
        "LOOPZERO_CONFORMANCE_MAX_UNACCOUNTED_RUNS",
        str(DEFAULT_MAX_UNACCOUNTED_RUNS),
    ))
    if max_unaccounted_runs <= 0:
        raise pytest.UsageError(
            "LOOPZERO_CONFORMANCE_MAX_UNACCOUNTED_RUNS must be positive"
        )
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
    unaccounted_runs = 0
    failures: list[str] = []
    with (
        _suite_access_only_credential(vendor, tmp_path),
        base_settings.use(),
        private_temporary_directory(f"{vendor}-suite-session") as session_home,
    ):
        settings = replace(base_settings, session_home=session_home)
        wrapper = _sandbox_wrapper(settings)
        reported_version = _version(
            executable, vendor, settings=settings, wrapper=wrapper
        )
        assert reported_version == PINS[vendor]
        readiness_runner = RealProcess(wrapper, vendor, "diagnose")
        readiness_adapter = RUNTIME_REGISTRY.create(
            vendor,
            settings=settings,
            run_cli=readiness_runner,
            run_probe=readiness_runner,
            **(
                {"sdk_available": lambda *_: True}
                if vendor in {"claude", "codex"}
                else {}
            ),
        )
        readiness = _probe_with_credential(
            vendor,
            readiness_adapter,
            _request(vendor, "success", tmp_path, scenario_timeout),
            timeout_s=scenario_timeout + 10,
            settings=settings,
            wrapper=wrapper,
        )
        readiness_record = _normalized_readiness(readiness)
        assert not readiness_runner.scenario_started
        if pytestconfig.getoption("live_diagnose"):
            _print_readiness(vendor, reported_version, readiness)
            assert readiness.ready, readiness.repair or "live runtime is not ready"
            return
        if not readiness.ready:
            records.append({
                "scenario": "preflight",
                "outcome": "not-ready",
                "accounting": "not-launched",
                "charged_cost_usd": 0.0,
                "readiness": readiness_record,
            })
            _write_results(
                vendor,
                reported_version,
                records,
                charged_cost,
                killed_runs=killed_runs,
                max_killed_runs=max_killed_runs,
                unaccounted_runs=unaccounted_runs,
                max_unaccounted_runs=max_unaccounted_runs,
                readiness=readiness_record,
            )
            pytest.fail(readiness.repair or "live runtime is not ready")

        for scenario in SCENARIOS:
            stop_reason = _suite_stop_reason(
                charged_cost=charged_cost,
                ceiling=ceiling,
                killed_runs=killed_runs,
                max_killed_runs=max_killed_runs,
                unaccounted_runs=unaccounted_runs,
                max_unaccounted_runs=max_unaccounted_runs,
            )
            if stop_reason is not None:
                record: dict[str, object] = {
                    "scenario": scenario,
                    "outcome": stop_reason,
                }
                if stop_reason == "aborted-killed-limit":
                    record["killed_runs"] = killed_runs
                elif stop_reason == "aborted-unaccounted-limit":
                    record["unaccounted_runs"] = unaccounted_runs
                records.append(record)
                break
            unsupported_reason = _unsupported_reason(vendor, scenario)
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
            invocation_sources: list[RuntimeCostSource] = []
            invocation_modes: list[RuntimeBillingMode] = []
            result: RuntimeResult | None = None
            try:
                invocation_costs.append(None)
                invocation_sources.append(RuntimeCostSource.UNKNOWN)
                invocation_modes.append(RuntimeBillingMode.UNKNOWN)
                result = _run_with_credential(
                    vendor,
                    adapter,
                    _request(vendor, scenario, tmp_path, timeout),
                    timeout_s=scenario_timeout + 10,
                    settings=scenario_settings,
                    wrapper=wrapper,
                )
                invocation_modes[-1] = result.billing_mode
                if result.cost_source in {
                    RuntimeCostSource.VENDOR,
                    RuntimeCostSource.ESTIMATED,
                }:
                    observed_cost = result.cost_usd
                    invocation_sources[-1] = result.cost_source
                else:
                    observed_cost = runner.metered_cost
                    invocation_sources[-1] = runner.metered_cost_source
                invocation_costs[-1] = observed_cost
                if scenario == "restart-resume" and result.session_id is not None:
                    resumed_adapter = RUNTIME_REGISTRY.create(
                        vendor, settings=scenario_settings, run_cli=runner, run_probe=runner,
                        **({"sdk_available": lambda *_: True} if vendor in {"claude", "codex"} else {}),
                    )
                    invocation_costs.append(None)
                    invocation_sources.append(RuntimeCostSource.UNKNOWN)
                    invocation_modes.append(RuntimeBillingMode.UNKNOWN)
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
                    invocation_modes[-1] = resumed.billing_mode
                    if resumed.cost_source in {
                        RuntimeCostSource.VENDOR,
                        RuntimeCostSource.ESTIMATED,
                    }:
                        resumed_cost = resumed.cost_usd
                        invocation_sources[-1] = resumed.cost_source
                    else:
                        resumed_cost = runner.metered_cost
                        invocation_sources[-1] = runner.metered_cost_source
                    invocation_costs[-1] = resumed_cost
                    original_session_id = result.session_id
                    result = resumed
                    observed_cost = (
                        observed_cost + resumed_cost
                        if observed_cost is not None and resumed_cost is not None
                        else None
                    )
                scenario_charge, accounting, scenario_unaccounted = _account_invocations(
                    vendor,
                    scenario,
                    invocation_costs,
                    scenario_budget,
                    cost_sources=invocation_sources,
                    prompt_bytes=prompt_bytes,
                )
                charged_cost += scenario_charge
                unaccounted_runs += scenario_unaccounted
                unsupported_reason = _post_launch_unsupported_reason(scenario, result)
                if unsupported_reason is None:
                    _assert_contract(scenario, result)
                if scenario == "restart-resume":
                    assert result.session_id == original_session_id
                record: dict[str, object] = {
                    "scenario": scenario,
                    "outcome": "unsupported" if unsupported_reason else "passed",
                    "accounting": accounting,
                    "charged_cost_usd": scenario_charge,
                    "cost_sources": [source.value for source in invocation_sources],
                    "billing_modes": [mode.value for mode in invocation_modes],
                    "result": _normalized(result),
                }
                if vendor == "codex" and scenario == "permission-denial":
                    record["observed_tool_outcomes"] = _observed_tool_outcomes(result)
                    record["denial_item_ids"] = [
                        event.item_id for event in result.events
                        if event.kind == "tool" and event.subtype == "denied:/etc/shadow"
                        and event.item_id is not None
                    ]
                if unsupported_reason is not None:
                    record["status"] = "unsupported"
                    record["reason"] = unsupported_reason
                if observed_cost is not None:
                    record["known_cost_usd"] = observed_cost
                records.append(record)
            except Exception as exc:
                if scenario_charge is None and runner.scenario_started:
                    started_costs = invocation_costs[:runner.scenario_invocations]
                    scenario_charge, accounting, scenario_unaccounted = (
                        _account_invocations(
                            vendor,
                            scenario,
                            started_costs,
                            scenario_budget,
                            cost_sources=invocation_sources[:runner.scenario_invocations],
                            prompt_bytes=prompt_bytes,
                        )
                    )
                    charged_cost += scenario_charge
                    unaccounted_runs += scenario_unaccounted
                failures.append(f"{scenario}: {type(exc).__name__}")
                failed_record: dict[str, object] = {
                    "scenario": scenario,
                    "outcome": "failed",
                    "failure_class": type(exc).__name__,
                    "billing_modes": [
                        mode.value for mode in invocation_modes[:runner.scenario_invocations]
                    ],
                    "accounting": accounting,
                    "charged_cost_usd": scenario_charge,
                    "cost_sources": [
                        source.value
                        for source in invocation_sources[:runner.scenario_invocations]
                    ],
                }
                if result is not None:
                    # Contract assertion failures must not erase the bounded
                    # SDK readiness/fallback evidence that explains the result.
                    failed_record["result"] = _normalized(result)
                records.append(failed_record)
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
        unaccounted_runs=unaccounted_runs,
        max_unaccounted_runs=max_unaccounted_runs,
        readiness=readiness_record,
    )
    assert not failures, "; ".join(failures)
    assert charged_cost <= ceiling
