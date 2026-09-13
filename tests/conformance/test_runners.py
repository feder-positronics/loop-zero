"""Seven scenario contracts through fake and env-selected native adapters."""

from dataclasses import dataclass, replace
import json
import os

import pytest

from loopzero.runners import claude, codex, cursor
from loopzero.runners.contract import (
    RuntimeCapabilityProfile, RuntimeEvent, RuntimeReadiness, RuntimeRequest,
    RuntimeStatus as Status, SubscriptionEligibility, TerminalReason as Reason,
)
from loopzero.runners.fake import Scenario
from loopzero.runners.process import ProcessHandle, ProcessResult
from loopzero.runners.registry import RUNTIME_REGISTRY


FAKE_EXPECTED = {
    Scenario.SUCCESS: (Status.COMPLETED, Reason.COMPLETED, ()),
    Scenario.MALFORMED_OUTPUT: (Status.FAILED, Reason.PROTOCOL_FAILURE, ("malformed-output",)),
    Scenario.TIMEOUT: (Status.TIMED_OUT, Reason.TIMEOUT, ("timeout",)),
    Scenario.PERMISSION_DENIAL: (Status.FAILED, Reason.PROCESS_EXIT, ("permission-denial",)),
    Scenario.CANCELLATION: (Status.CANCELLED, Reason.CANCELLED, ("cancellation",)),
    Scenario.DISCONNECT: (Status.FAILED, Reason.TRANSPORT_DISCONNECT, ("disconnect",)),
    Scenario.RESTART_RESUME: (Status.COMPLETED, Reason.COMPLETED, ()),
}
INIT = RuntimeEvent("system", "init")
MESSAGE = RuntimeEvent("assistant", "message", True)
RESULT = RuntimeEvent("result", "success", True)
PERMISSION = RuntimeEvent("permission", "denied", True)


def request(tmp_path, vendor="fake"):
    return RuntimeRequest(
        vendor=vendor,
        transport=RUNTIME_REGISTRY.registration(vendor).preferred_transport,
        requested_model="test-model",
        effort="high",
        prompt="private prompt",
        cwd=tmp_path,
        timeout_s=1,
        read_only=True,
        attempt_id="scenario-attempt",
        eligibility=SubscriptionEligibility.APPROVED,
        capability_profile=RuntimeCapabilityProfile(read_roots=(tmp_path,)),
        output_schema={"type": "object"},
    )


def _run_fake_scenario(scenario, tmp_path):
    configured = Scenario.SUCCESS if scenario is Scenario.CANCELLATION else scenario
    adapter = RUNTIME_REGISTRY.create("fake", scenario={"scenario": configured.value})
    req = request(tmp_path)
    assert adapter.probe(req).ready
    progress = []

    def observe(item):
        progress.append(item)
        if scenario is Scenario.CANCELLATION:
            adapter.cancel(adapter.handle)

    outcome = adapter.run(req, on_progress=observe)
    if scenario is Scenario.RESTART_RESUME:
        assert (outcome.status, outcome.terminal_reason) == (
            Status.FAILED, Reason.TRANSPORT_DISCONNECT,
        )
        assert outcome.diagnostics == ("disconnect",)
        checkpoint = adapter.checkpoint
        assert checkpoint is not None
        assert checkpoint.events == (INIT, MESSAGE)
        adapter = RUNTIME_REGISTRY.create(
            "fake", scenario=scenario.value, checkpoint=checkpoint
        )
        outcome = adapter.run(req, on_progress=observe)
    status, reason, diagnostics = FAKE_EXPECTED[scenario]
    assert (outcome.status, outcome.terminal_reason) == (status, reason)
    assert outcome.diagnostics == diagnostics
    assert outcome.final_output == (
        "deterministic result" if status is Status.COMPLETED else None
    )
    expected_events = (INIT, MESSAGE)
    if scenario in (Scenario.CANCELLATION, Scenario.TIMEOUT):
        expected_events = (INIT,)
    elif scenario in (Scenario.SUCCESS, Scenario.RESTART_RESUME):
        expected_events += (RESULT,)
    elif scenario is Scenario.PERMISSION_DENIAL:
        expected_events += (PERMISSION,)
    assert outcome.events == expected_events
    assert [item.sequence for item in progress] == list(range(1, len(progress) + 1))
    assert "private prompt" not in repr(outcome)


def test_resume_rejects_a_different_attempt(tmp_path):
    adapter = RUNTIME_REGISTRY.create("fake", scenario=Scenario.RESTART_RESUME)
    adapter.run(request(tmp_path))
    resumed = RUNTIME_REGISTRY.create(
        "fake", scenario=Scenario.RESTART_RESUME, checkpoint=adapter.checkpoint
    )
    with pytest.raises(ValueError, match="checkpoint does not match"):
        resumed.run(replace(request(tmp_path), attempt_id="another-attempt"))


@dataclass(frozen=True)
class NativeReplayCheckpoint:
    vendor: str
    attempt_id: str
    session_id: str
    events: tuple[RuntimeEvent, ...]


def _native_wire(
    vendor: str,
    scenario: Scenario,
    *,
    resumed_session_id: str | None = None,
) -> ProcessResult:
    is_cursor = vendor == "cursor"
    session_id = resumed_session_id or "native-checkpoint"
    initial = (
        [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "subtype": "message", "session_id": session_id},
        ]
        if is_cursor
        else [
            {"type": "event", "kind": "system", "subtype": "init"},
            {
                "type": "event", "kind": "assistant", "subtype": "message",
                "semantic": True, "session_id": session_id,
            },
        ]
    )
    frames = list(initial)
    if resumed_session_id is not None:
        frames.append(
            (
                {
                    "type": "system",
                    "subtype": f"resume-{resumed_session_id}",
                    "session_id": session_id,
                }
                if is_cursor
                else {
                    "type": "event",
                    "kind": "system",
                    "subtype": f"resume-{resumed_session_id}",
                    "session_id": session_id,
                }
            )
        )
    if scenario is Scenario.MALFORMED_OUTPUT:
        text = "\n".join(map(json.dumps, frames)) + "\ninvalid-json"
    elif scenario is Scenario.RESTART_RESUME and is_cursor and resumed_session_id is None:
        frames.append(
            {
                "type": "result", "subtype": "error", "is_error": True,
                "result": "checkpointed", "session_id": session_id,
            }
        )
        text = "\n".join(map(json.dumps, frames))
    elif scenario in {
        Scenario.DISCONNECT, Scenario.RESTART_RESUME, Scenario.CANCELLATION,
    } and resumed_session_id is None:
        if not is_cursor:
            frames.append({"type": "error", "reason": "disconnect"})
        text = "\n".join(map(json.dumps, frames))
    else:
        denied = scenario is Scenario.PERMISSION_DENIAL
        if denied:
            frames.append(
                {"type": "permission", "subtype": "denied"}
                if is_cursor
                else {
                    "type": "event", "kind": "permission", "subtype": "denied",
                    "semantic": True,
                }
            )
        terminal = {
            "type": "result", "subtype": "error" if denied else "success",
            "is_error": denied,
            "result": "denied" if denied else "deterministic result",
            "session_id": session_id,
        }
        if not is_cursor:
            terminal.update(
                status="failed" if denied else "completed",
                terminal_reason="process-exit" if denied else "completed",
            )
            if denied and vendor == "claude":
                terminal.update(permission_denial_count=1, permission_denial_tools=["Bash"])
        frames.append(terminal)
        text = "\n".join(map(json.dumps, frames))
    return ProcessResult(
        returncode=0, stdout=text, stderr="", duration_s=0.0,
        timed_out=scenario is Scenario.TIMEOUT,
    )


def _native_expected(vendor: str, scenario: Scenario):
    label = {
        "claude": "Claude SDK bridge",
        "codex": "Codex SDK bridge",
        "cursor": "Cursor process",
    }[vendor]
    if scenario in {Scenario.SUCCESS, Scenario.RESTART_RESUME}:
        return Status.COMPLETED, Reason.COMPLETED, ()
    if scenario is Scenario.TIMEOUT:
        return Status.TIMED_OUT, Reason.TIMEOUT, (f"{label} timed out",)
    if scenario is Scenario.PERMISSION_DENIAL:
        diagnostics = ("Claude permission denials: 1: Bash",) if vendor == "claude" else ()
        return Status.FAILED, Reason.PROCESS_EXIT, diagnostics
    if scenario is Scenario.MALFORMED_OUTPUT:
        if vendor == "cursor":
            return Status.FAILED, Reason.MALFORMED_EVENT, (
                "cursor emitted malformed stream-json", "Cursor parser cause: invalid-json",
            )
        return Status.FAILED, Reason.PROTOCOL_FAILURE, (
            f"{label} emitted malformed protocol",
        )
    if vendor == "cursor":
        return Status.FAILED, Reason.MALFORMED_EVENT, (
            "cursor emitted malformed stream-json", "Cursor parser cause: terminal-missing",
        )
    return Status.FAILED, Reason.TRANSPORT_DISCONNECT, (
        f"{vendor.title()} SDK transport disconnected",
    )


SELECTED_VENDOR = os.environ.get("LOOPZERO_CONFORMANCE_RUNNER", "fake")
assert SELECTED_VENDOR in {"fake", "claude", "codex", "cursor"}
DEFAULT_CASES = tuple((SELECTED_VENDOR, scenario) for scenario in Scenario)


def test_default_selection_contains_all_seven_fake_scenarios():
    if "LOOPZERO_CONFORMANCE_RUNNER" in os.environ:
        pytest.skip("real vendor was explicitly selected")
    assert DEFAULT_CASES == tuple(("fake", scenario) for scenario in Scenario)
    assert len(DEFAULT_CASES) == 7


@pytest.mark.parametrize("vendor,scenario", DEFAULT_CASES)
def test_selected_adapter_protocol_scenario(vendor, scenario, tmp_path, monkeypatch):
    if vendor == "fake":
        _run_fake_scenario(scenario, tmp_path)
        return
    req = request(tmp_path, vendor)
    calls = []
    outcomes = [_native_wire(vendor, scenario)]
    cancel_calls = []
    adapter = None

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        if scenario is Scenario.CANCELLATION:
            assert adapter is not None
            adapter.cancel(ProcessHandle(process=None, pid=41, pgid=41))  # type: ignore[arg-type]
        return outcomes.pop(0)

    adapter = RUNTIME_REGISTRY.create(
        vendor, run_cli=transport, run_probe=transport, which=lambda _: "/native"
    )
    module = {"claude": claude, "codex": codex, "cursor": cursor}[vendor]
    monkeypatch.setattr(module, "cancel_cli", cancel_calls.append)
    ready = lambda selected: RuntimeReadiness(True, selected.eligibility)
    if vendor == "claude":
        monkeypatch.setattr(adapter, "probe_sdk", ready)
    elif vendor == "codex":
        monkeypatch.setattr(adapter, "_sdk_launch_readiness", ready)
    else:
        monkeypatch.setattr(adapter, "probe", ready)

    result = adapter.run(req)
    if scenario is Scenario.CANCELLATION:
        assert len(cancel_calls) == 1
    if scenario is Scenario.RESTART_RESUME:
        assert (result.status, result.terminal_reason) == (
            Status.FAILED,
            Reason.PROCESS_EXIT if vendor == "cursor" else Reason.TRANSPORT_DISCONNECT,
        )
        assert result.session_id == "native-checkpoint"
        checkpoint = NativeReplayCheckpoint(
            vendor, req.attempt_id, result.session_id, result.events
        )
        assert [(event.kind, event.subtype) for event in checkpoint.events[:2]] == [
            ("system", "init"), ("assistant", "message"),
        ]
        resume_calls = []

        def resume_transport(*args, **kwargs):
            resume_calls.append((args, kwargs))
            if vendor == "cursor":
                command = list(args[0])
                consumed_session_id = command[command.index("--resume") + 1]
            else:
                consumed_session_id = json.loads(kwargs["input_text"])[
                    "resume_session_id"
                ]
            assert consumed_session_id == checkpoint.session_id
            return _native_wire(
                vendor,
                scenario,
                resumed_session_id=consumed_session_id,
            )

        adapter = RUNTIME_REGISTRY.create(
            vendor, run_cli=resume_transport, run_probe=resume_transport,
            which=lambda _: "/native",
        )
        if vendor == "claude":
            monkeypatch.setattr(adapter, "probe_sdk", ready)
        elif vendor == "codex":
            monkeypatch.setattr(adapter, "_sdk_launch_readiness", ready)
        else:
            monkeypatch.setattr(adapter, "probe", ready)
        resumed_request = replace(req, resume_session_id=checkpoint.session_id)
        result = adapter.run(resumed_request)
        assert len(resume_calls) == 1
        assert result.session_id == checkpoint.session_id
        assert any(
            event.kind == "system"
            and event.subtype == f"resume-{checkpoint.session_id}"
            for event in result.events
        )
    assert len(calls) == 1
    status, reason, diagnostics = _native_expected(vendor, scenario)
    assert (result.status, result.terminal_reason) == (status, reason)
    assert result.diagnostics == diagnostics
    if scenario is Scenario.PERMISSION_DENIAL:
        assert any(
            event.kind == "permission" and event.subtype == "denied"
            for event in result.events
        )
    if scenario in {Scenario.SUCCESS, Scenario.RESTART_RESUME}:
        assert result.final_output == "deterministic result"
        expected_kinds = ["system", "assistant"]
        if scenario is Scenario.RESTART_RESUME:
            expected_kinds.append("system")
        expected_kinds.append("result")
        assert [event.kind for event in result.events] == expected_kinds
    assert "private prompt" not in repr(result)
