"""Scenario contract through the registry; optional native protocol replay.

The real-adapter lane uses deterministic transport responses, not billable live
turns. It keeps native limitations visible (notably cancellation and resume)
without pretending the cutover added protocol capabilities.
"""

from dataclasses import replace
import json
import os

import pytest

from loopzero.runners.contract import (
    RuntimeCapabilityProfile, RuntimeEvent, RuntimeReadiness, RuntimeRequest,
    RuntimeStatus as Status, SubscriptionEligibility, TerminalReason as Reason,
)
from loopzero.runners.fake import Scenario
from loopzero.runners.process import ProcessResult
from loopzero.runners.registry import RUNTIME_REGISTRY


EXPECTED = {
    Scenario.SUCCESS: (Status.COMPLETED, Reason.COMPLETED, None),
    Scenario.MALFORMED_OUTPUT: (Status.FAILED, Reason.PROTOCOL_FAILURE, "malformed-output"),
    Scenario.TIMEOUT: (Status.TIMED_OUT, Reason.TIMEOUT, "timeout"),
    Scenario.PERMISSION_DENIAL: (Status.FAILED, Reason.PROCESS_EXIT, "permission-denial"),
    Scenario.CANCELLATION: (Status.CANCELLED, Reason.CANCELLED, "cancellation"),
    Scenario.DISCONNECT: (Status.FAILED, Reason.TRANSPORT_DISCONNECT, "disconnect"),
    Scenario.RESTART_RESUME: (Status.COMPLETED, Reason.COMPLETED, None),
}
INIT = RuntimeEvent("system", "init")
MESSAGE = RuntimeEvent("assistant", "message", True)
RESULT = RuntimeEvent("result", "success", True)


def request(tmp_path, vendor="fake"):
    return RuntimeRequest(
        vendor=vendor, transport=RUNTIME_REGISTRY.registration(vendor).preferred_transport,
        requested_model="test-model", effort="high", prompt="private prompt",
        cwd=tmp_path, timeout_s=1, read_only=True, attempt_id="scenario-attempt",
        eligibility=SubscriptionEligibility.APPROVED,
        capability_profile=RuntimeCapabilityProfile(read_roots=(tmp_path,)),
        output_schema={"type": "object"},
    )


@pytest.mark.parametrize("scenario", list(Scenario))
def test_fake_scenario(scenario, tmp_path):
    adapter = RUNTIME_REGISTRY.create("fake", scenario={"scenario": scenario.value})
    req = request(tmp_path)
    assert adapter.probe(req).ready
    progress = []
    outcome = adapter.run(req, on_progress=progress.append)
    if scenario is Scenario.RESTART_RESUME:
        assert outcome.status is Status.FAILED
        assert outcome.terminal_reason is Reason.TRANSPORT_DISCONNECT
        assert outcome.diagnostics == ("disconnect",)
        assert outcome.events == (INIT, MESSAGE)
        # Resume in a genuinely new adapter, retaining sequence and history.
        adapter = RUNTIME_REGISTRY.create("fake", scenario=scenario.value, checkpoint=adapter.checkpoint)
        outcome = adapter.run(req, on_progress=progress.append)
    status, reason, category = EXPECTED[scenario]
    assert (outcome.status, outcome.terminal_reason) == (status, reason)
    assert outcome.diagnostics == ((category,) if category else ())
    assert outcome.final_output == ("deterministic result" if status is Status.COMPLETED else None)
    expected_events = (INIT, MESSAGE)
    if scenario in (Scenario.CANCELLATION, Scenario.TIMEOUT):
        expected_events = (INIT,)
    elif scenario in (Scenario.SUCCESS, Scenario.RESTART_RESUME):
        expected_events += (RESULT,)
    elif scenario is Scenario.PERMISSION_DENIAL:
        expected_events += (RuntimeEvent("permission", "denied", True),)
    assert outcome.events == expected_events
    assert [p.sequence for p in progress] == list(range(1, len(progress) + 1))
    assert "private prompt" not in repr(outcome)


def test_cancellation_at_a_progress_transition_and_expired_request(tmp_path):
    adapter = RUNTIME_REGISTRY.create("fake")
    result = adapter.run(request(tmp_path), on_progress=lambda _: adapter.cancel(adapter.handle))
    assert result.status is Status.CANCELLED
    assert result.events == (INIT,)
    result = RUNTIME_REGISTRY.create("fake").run(replace(request(tmp_path), timeout_s=0))
    assert result.terminal_reason is Reason.TIMEOUT
    assert result.events == (INIT,)


def test_resume_rejects_a_different_attempt(tmp_path):
    adapter = RUNTIME_REGISTRY.create("fake", scenario=Scenario.RESTART_RESUME)
    adapter.run(request(tmp_path))
    resumed = RUNTIME_REGISTRY.create("fake", scenario=Scenario.RESTART_RESUME, checkpoint=adapter.checkpoint)
    with pytest.raises(ValueError, match="checkpoint does not match"):
        resumed.run(replace(request(tmp_path), attempt_id="another-attempt"))


@pytest.mark.parametrize("scenario", list(Scenario))
def test_selected_native_adapter_protocol_replay(scenario, tmp_path, monkeypatch):
    vendor = os.environ.get("LOOPZERO_CONFORMANCE_RUNNER")
    if vendor is None:
        pytest.skip("set LOOPZERO_CONFORMANCE_RUNNER=claude|codex|cursor for native replay")
    assert vendor in {"claude", "codex", "cursor"}
    req = request(tmp_path, vendor)
    calls = []
    outcomes = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return outcomes.pop(0)

    adapter = RUNTIME_REGISTRY.create(vendor, run_cli=transport, run_probe=transport, which=lambda _: "/native")
    ready = lambda request: RuntimeReadiness(True, request.eligibility)
    # Unit tests own auth/readiness. Here the process seam is the fault injector.
    if vendor == "claude":
        monkeypatch.setattr(adapter, "probe_sdk", ready)
    elif vendor == "codex":
        monkeypatch.setattr(adapter, "_sdk_launch_readiness", ready)
    else:
        monkeypatch.setattr(adapter, "probe", ready)

    def wire(kind):
        cursor = vendor == "cursor"
        initial = ([{"type": "system", "subtype": "init"},
                    {"type": "assistant", "subtype": "message"}] if cursor else
                   [{"type": "event", "kind": "system", "subtype": "init"},
                    {"type": "event", "kind": "assistant", "subtype": "message", "semantic": True}])
        frames = initial.copy()
        if kind is Scenario.MALFORMED_OUTPUT:
            text = "\n".join(map(json.dumps, frames)) + "\ninvalid-json"
        elif kind in (Scenario.DISCONNECT, Scenario.RESTART_RESUME, Scenario.CANCELLATION):
            if not cursor:
                frames.append({"type": "error", "reason": "disconnect"})
            text = "\n".join(map(json.dumps, frames))
        else:
            failed = kind is Scenario.PERMISSION_DENIAL
            terminal = {"type": "result", "subtype": "error" if failed else "success",
                        "is_error": failed, "result": "denied" if failed else "deterministic result"}
            if not cursor:
                terminal.update(status="failed" if failed else "completed",
                                terminal_reason="process-exit" if failed else "completed")
            frames.append(terminal)
            text = "\n".join(map(json.dumps, frames))
        return ProcessResult(returncode=0, stdout=text, stderr="", duration_s=0.0,
                             timed_out=kind is Scenario.TIMEOUT)

    outcomes.append(wire(scenario))
    result = adapter.run(req)
    assert len(calls) == 1  # semantic faults must never cause a hidden retry
    if scenario is Scenario.RESTART_RESUME:
        assert result.status is Status.FAILED
        # The legacy contract has no resume token. The owner explicitly starts
        # a new invocation; this test does not claim native session resumption.
        outcomes.append(wire(Scenario.SUCCESS))
        result = adapter.run(req)
        assert len(calls) == 2
    if scenario in (Scenario.SUCCESS, Scenario.RESTART_RESUME):
        assert (result.status, result.terminal_reason) == (Status.COMPLETED, Reason.COMPLETED)
        assert result.final_output == "deterministic result"
        assert [e.kind for e in result.events] == ["system", "assistant", "result"]
    elif scenario is Scenario.TIMEOUT:
        assert (result.status, result.terminal_reason) == (Status.TIMED_OUT, Reason.TIMEOUT)
        assert result.events == ()
    elif scenario is Scenario.PERMISSION_DENIAL:
        assert (result.status, result.terminal_reason) == (Status.FAILED, Reason.PROCESS_EXIT)
        assert [e.kind for e in result.events] == ["system", "assistant", "result"]
    elif scenario is Scenario.MALFORMED_OUTPUT or vendor == "cursor":
        expected = Reason.MALFORMED_EVENT if vendor == "cursor" else Reason.PROTOCOL_FAILURE
        assert (result.status, result.terminal_reason) == (Status.FAILED, expected)
        assert result.events == ()
    else:
        # The source bridge reports cancellation as disconnect; preserve it.
        assert (result.status, result.terminal_reason) == (Status.FAILED, Reason.TRANSPORT_DISCONNECT)
        assert [e.kind for e in result.events][:2] == ["system", "assistant"]
    category = {Reason.COMPLETED: None, Reason.TIMEOUT: "timeout", Reason.PROCESS_EXIT: "process-exit",
                Reason.MALFORMED_EVENT: "protocol", Reason.PROTOCOL_FAILURE: "protocol",
                Reason.TRANSPORT_DISCONNECT: "disconnect"}[result.terminal_reason]
    expected_category = {
        Scenario.SUCCESS: None, Scenario.RESTART_RESUME: None,
        Scenario.TIMEOUT: "timeout", Scenario.PERMISSION_DENIAL: "process-exit",
        Scenario.MALFORMED_OUTPUT: "protocol",
        Scenario.CANCELLATION: "protocol" if vendor == "cursor" else "disconnect",
        Scenario.DISCONNECT: "protocol" if vendor == "cursor" else "disconnect",
    }[scenario]
    assert category == expected_category
    assert "private prompt" not in repr(result)
