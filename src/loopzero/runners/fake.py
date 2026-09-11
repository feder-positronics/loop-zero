"""Deterministic runtime scenarios with no processes, clocks, SDKs or credentials.

Restart/resume uses an explicit checkpoint that can be transferred to a newly
constructed adapter. Progress callbacks run synchronously, so a test can cancel
at an exact transition. Error categories are scenario expectations, not changes
to the legacy RuntimeResult wire contract.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .contract import (
    RuntimeEvent, RuntimeHandle, RuntimePhase, RuntimeProgress,
    RuntimeProgressCallback, RuntimeProgressSignal, RuntimeReadiness,
    RuntimeRequest, RuntimeResult, RuntimeStatus, TerminalReason,
)


class Scenario(StrEnum):
    SUCCESS = "success"
    MALFORMED_OUTPUT = "malformed-output"
    TIMEOUT = "timeout/expiry"
    PERMISSION_DENIAL = "permission-denial"
    CANCELLATION = "cancellation"
    DISCONNECT = "disconnect mid-stream"
    RESTART_RESUME = "restart/resume"


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    scenario: Scenario = Scenario.SUCCESS
    output: str = "deterministic result"

    @classmethod
    def from_mapping(cls, spec: Mapping[str, object]) -> "ScenarioSpec":
        if set(spec) - {"scenario", "output"}:
            raise ValueError("unknown fake scenario field")
        output = spec.get("output", "deterministic result")
        if not isinstance(output, str):
            raise ValueError("fake output must be a string")
        return cls(Scenario(spec.get("scenario", "success")), output)


@dataclass(frozen=True, slots=True)
class FakeCheckpoint:
    attempt_id: str
    sequence: int
    events: tuple[RuntimeEvent, ...]


class FakeAdapter:
    """The same probe/run/cancel contract as the native transports."""

    transport = "fake/scenario"
    handle = RuntimeHandle(pid=0, transport=transport)

    def __init__(
        self, *, scenario: ScenarioSpec | Mapping[str, object] | str = "success",
        checkpoint: FakeCheckpoint | None = None,
    ) -> None:
        if isinstance(scenario, str):
            scenario = ScenarioSpec(Scenario(scenario))
        elif not isinstance(scenario, ScenarioSpec):
            scenario = ScenarioSpec.from_mapping(scenario)
        self.scenario = scenario
        self.checkpoint = checkpoint
        self._cancelled = False

    def probe(self, request: RuntimeRequest) -> RuntimeReadiness:
        return RuntimeReadiness(True, request.eligibility, transport=self.transport)

    def cancel(self, handle: RuntimeHandle) -> None:
        if handle != self.handle:
            raise ValueError("fake handle does not belong to this transport")
        self._cancelled = True

    def run(
        self, request: RuntimeRequest, *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult:
        scenario = self.scenario.scenario
        checkpoint = self.checkpoint
        if checkpoint is not None and (
            scenario is not Scenario.RESTART_RESUME
            or checkpoint.attempt_id != request.attempt_id
        ):
            raise ValueError("fake checkpoint does not match the attempt")
        events = list(checkpoint.events) if checkpoint else []
        sequence = checkpoint.sequence if checkpoint else 0

        def emit(phase: RuntimePhase, event: RuntimeEvent) -> None:
            nonlocal sequence
            events.append(event)
            sequence += 1
            if on_progress:
                on_progress(RuntimeProgress(
                    phase, RuntimeProgressSignal.TRANSITION, sequence, 0.0,
                ))

        def finish(status, reason, category=None, output=None):
            return RuntimeResult(
                vendor=request.vendor, transport=self.transport,
                requested_model=request.requested_model, attempt_id=request.attempt_id,
                eligibility=request.eligibility, status=status, terminal_reason=reason,
                events=tuple(events), diagnostics=(category,) if category else (),
                final_output=output, duration_s=0.0,
                returncode=0 if status is RuntimeStatus.COMPLETED else 1,
                session_id="fake-session",
            )

        if not checkpoint:
            emit(RuntimePhase.STARTUP, RuntimeEvent("system", "init"))
        if self._cancelled or scenario is Scenario.CANCELLATION:
            return finish(RuntimeStatus.CANCELLED, TerminalReason.CANCELLED, "cancellation")
        if request.timeout_s <= 0 or scenario is Scenario.TIMEOUT:
            return finish(RuntimeStatus.TIMED_OUT, TerminalReason.TIMEOUT, "timeout")
        if not checkpoint:
            emit(RuntimePhase.MODEL_ACTIVE, RuntimeEvent("assistant", "message", True))
        if self._cancelled:
            return finish(RuntimeStatus.CANCELLED, TerminalReason.CANCELLED, "cancellation")
        if scenario is Scenario.MALFORMED_OUTPUT:
            return finish(RuntimeStatus.FAILED, TerminalReason.PROTOCOL_FAILURE, "malformed-output")
        if scenario is Scenario.PERMISSION_DENIAL:
            emit(RuntimePhase.TOOL_EXECUTION, RuntimeEvent("permission", "denied", True))
            return finish(RuntimeStatus.FAILED, TerminalReason.PROCESS_EXIT, "permission-denial")
        if scenario is Scenario.DISCONNECT or (scenario is Scenario.RESTART_RESUME and not checkpoint):
            if scenario is Scenario.RESTART_RESUME:
                self.checkpoint = FakeCheckpoint(request.attempt_id, sequence, tuple(events))
            return finish(RuntimeStatus.FAILED, TerminalReason.TRANSPORT_DISCONNECT, "disconnect")
        emit(RuntimePhase.RESULT_PACKAGING, RuntimeEvent("result", "success", True))
        return finish(RuntimeStatus.COMPLETED, TerminalReason.COMPLETED, output=self.scenario.output)
