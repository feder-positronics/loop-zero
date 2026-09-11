"""Immutable, vendor-neutral contracts for native agent runtimes."""

from ._review_schema import review_sections_schema, validate_review_chain_task

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

RUNTIME_PROGRESS_PROTOCOL_VERSION = 1
MAX_STRUCTURED_OUTPUT_BYTES = 8 * 1024 * 1024
# A CLI may JSON-escape every non-BMP code point (3x UTF-8 expansion) and
# duplicate the governed object in both text and structured terminal fields.
MAX_PROTOCOL_LINE_BYTES = 6 * MAX_STRUCTURED_OUTPUT_BYTES + 64 * 1024


class SubscriptionEligibility(StrEnum):
    """Subscription evidence supplied by repository policy or native tooling."""

    APPROVED = "approved"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


class ReadinessFailure(StrEnum):
    """Closed readiness failures that may inform same-vendor selection."""

    EXECUTABLE_MISSING = "executable-missing"
    PROTOCOL_INCOMPATIBLE = "protocol-incompatible"
    AUTHENTICATION = "authentication"
    SUBSCRIPTION_UNAVAILABLE = "subscription-unavailable"
    UNSUPPORTED_PLATFORM = "unsupported-platform"
    SDK_UNAVAILABLE = "sdk-unavailable"
    SDK_VERSION_MISMATCH = "sdk-version-mismatch"
    MODEL_UNSUPPORTED = "model-unsupported"
    CONFIG_BOOTSTRAP = "config-bootstrap"


class RuntimeStatus(StrEnum):
    """Terminal runtime state; dispatcher policy interprets these outcomes."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"
    SUBSCRIPTION_UNAVAILABLE = "subscription-unavailable"


class RuntimeCostStatus(StrEnum):
    """Evidence strength for a normalized runtime cost value."""

    UNKNOWN = "unknown"
    ESTIMATED = "estimated"
    OBSERVED = "observed"


class RuntimeCommercialMode(StrEnum):
    """Owner-authorized commercial boundary for one native invocation."""

    SUBSCRIPTION_ONLY = "subscription-only"
    PROMOTIONAL_CREDIT = "promotional-credit"
    OWNER_PAID = "owner-paid"


class RuntimePhase(StrEnum):
    """Closed, content-free phases exposed by native SDK runtimes."""

    STARTUP = "startup"
    MODEL_ACTIVE = "model-active"
    TOOL_EXECUTION = "tool-execution"
    RESULT_PACKAGING = "result-packaging"


class RuntimeProgressSignal(StrEnum):
    """Closed progress signal vocabulary for transitions and liveness."""

    TRANSITION = "transition"
    HEARTBEAT = "heartbeat"


class RuntimeToolLabel(StrEnum):
    """Closed, content-free labels for the most recently observed SDK tool."""

    COMMAND = "command"
    FILE = "file"
    SEARCH = "search"
    WEB = "web"
    TOOL = "tool"


class RuntimeProgressProtocolError(ValueError):
    """A dedicated progress frame violated the content-free wire contract."""


class TerminalReason(StrEnum):
    """Bounded terminal reasons safe to retain in runtime telemetry."""

    COMPLETED = "completed"
    PROCESS_EXIT = "process-exit"
    MALFORMED_EVENT = "malformed-event"
    MISSING_TERMINAL_EVENT = "missing-terminal-event"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SUBSCRIPTION_UNAVAILABLE = "subscription-unavailable"
    STARTUP_FAILURE = "startup-failure"
    PROTOCOL_FAILURE = "protocol-failure"
    TRANSPORT_DISCONNECT = "transport-disconnect"
    MODEL_RESULT = "model-result"
    BUDGET_EXHAUSTED = "budget-exhausted"
    BUDGET_EXHAUSTED_AFTER_RESULT = "budget-exhausted-after-result"
    COMMERCIAL_BOUNDARY = "commercial-boundary"


class ModelResultReason(StrEnum):
    """Closed, content-free causes for rejected governed worker results."""

    WORKER_REPORTED_FAILURE = "worker-reported-failure"
    MISSING_STRUCTURED_OUTPUT = "missing-structured-output"
    POSTHOC_EXTRACTION_FAILURE = "posthoc-extraction-failure"
    SCHEMA_SHAPE_OR_BOUNDS = "schema-shape-or-bounds"
    TASK_ID_MISMATCH = "task-id-mismatch"
    UNSUPPORTED_STATUS = "unsupported-status"


class ResultEnforcement(StrEnum):
    """How the adapter supplied the governed result candidate."""

    NATIVE_SCHEMA = "native-schema"
    POSTHOC_EXTRACTION = "posthoc-extraction"


@dataclass(frozen=True, slots=True)
class RuntimeProgress:
    """Sanitized live progress with no native payload or identifying metadata."""

    phase: RuntimePhase
    signal: RuntimeProgressSignal
    sequence: int
    activity_age_s: float
    last_tool: RuntimeToolLabel | None = None


RuntimeProgressCallback = Callable[[RuntimeProgress], None]


def parse_runtime_progress_frame(frame: object) -> RuntimeProgress:
    """Strictly validate one decoded progress-pipe frame."""
    required_fields = {
        "version",
        "phase",
        "signal",
        "sequence",
        "activity_age_s",
    }
    if (
        not isinstance(frame, Mapping)
        or set(frame) not in (required_fields, required_fields | {"last_tool"})
    ):
        raise RuntimeProgressProtocolError("runtime progress frame shape is invalid")
    version = frame["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != RUNTIME_PROGRESS_PROTOCOL_VERSION
    ):
        raise RuntimeProgressProtocolError("runtime progress version is invalid")
    raw_phase = frame["phase"]
    raw_signal = frame["signal"]
    if not isinstance(raw_phase, str) or not isinstance(raw_signal, str):
        raise RuntimeProgressProtocolError("runtime progress vocabulary is invalid")
    try:
        phase = RuntimePhase(raw_phase)
        signal = RuntimeProgressSignal(raw_signal)
    except (TypeError, ValueError) as exc:
        raise RuntimeProgressProtocolError(
            "runtime progress vocabulary is invalid"
        ) from exc
    sequence = frame["sequence"]
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= 2**63 - 1
    ):
        raise RuntimeProgressProtocolError("runtime progress sequence is invalid")
    raw_activity_age = frame["activity_age_s"]
    if (
        isinstance(raw_activity_age, bool)
        or not isinstance(raw_activity_age, (int, float))
        or not math.isfinite(raw_activity_age)
        or not 0 <= raw_activity_age <= 10**9
    ):
        raise RuntimeProgressProtocolError("runtime progress activity age is invalid")
    raw_last_tool = frame.get("last_tool")
    if raw_last_tool is not None and not isinstance(raw_last_tool, str):
        raise RuntimeProgressProtocolError("runtime progress tool label is invalid")
    try:
        last_tool = (
            RuntimeToolLabel(raw_last_tool) if raw_last_tool is not None else None
        )
    except ValueError as exc:
        raise RuntimeProgressProtocolError(
            "runtime progress tool label is invalid"
        ) from exc
    return RuntimeProgress(
        phase=phase,
        signal=signal,
        sequence=sequence,
        activity_age_s=float(raw_activity_age),
        last_tool=last_tool,
    )


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityProfile:
    """Task-derived filesystem/tool boundary translated by native adapters."""

    read_roots: tuple[Path, ...] = ()
    evidence_read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    approved_commands: tuple[str, ...] = field(default=(), repr=False)
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeTransportAttempt:
    """Bounded evidence for one preferred or same-vendor backup transport."""

    transport: str
    requested_model: str
    phase: str
    status: str
    terminal_reason: str | None
    failure_class: str | None
    duration_s: float | None
    semantic: bool
    selected_next: bool


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """One already-routed native-runtime invocation.

    ``prompt`` is execution input, never telemetry.  Its repr is deliberately
    suppressed so accidental diagnostic logging cannot expose it.
    """

    vendor: Literal["claude", "codex", "cursor"]
    transport: str
    requested_model: str
    effort: str
    prompt: str = field(repr=False)
    cwd: Path = field(repr=False)
    timeout_s: float
    read_only: bool
    attempt_id: str
    tooling_root: Path | None = field(default=None, repr=False)
    fallback_from: str | None = None
    eligibility: SubscriptionEligibility = SubscriptionEligibility.AMBIGUOUS
    commercial_mode: RuntimeCommercialMode = RuntimeCommercialMode.SUBSCRIPTION_ONLY
    budget_usd: float | None = None
    output_schema: dict[str, object] | None = field(default=None, repr=False)
    capability_profile: RuntimeCapabilityProfile = field(
        default_factory=RuntimeCapabilityProfile,
        repr=False,
    )
    visible_tools: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    """Non-billable availability evidence for one selected transport."""

    ready: bool
    eligibility: SubscriptionEligibility
    failure: ReadinessFailure | None = None
    repair: str | None = None
    transport: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Sanitized event metadata only; native payloads are intentionally omitted."""

    kind: str
    subtype: str | None = None
    semantic: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeUsage:
    """Optional native usage counters; ``None`` means unavailable, not zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    """Opaque process identity used only for cancellation by the owner."""

    pid: int
    transport: str


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Normalized terminal evidence with no prompt, tool, or model-output body."""

    vendor: str
    transport: str
    requested_model: str
    status: RuntimeStatus
    terminal_reason: TerminalReason
    attempt_id: str
    effective_model: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    usage: RuntimeUsage | None = None
    cost_usd: float | None = None
    cost_status: RuntimeCostStatus = RuntimeCostStatus.UNKNOWN
    eligibility: SubscriptionEligibility = SubscriptionEligibility.AMBIGUOUS
    commercial_mode: RuntimeCommercialMode = RuntimeCommercialMode.SUBSCRIPTION_ONLY
    fallback_from: str | None = None
    events: tuple[RuntimeEvent, ...] = ()
    diagnostics: tuple[str, ...] = ()
    returncode: int | None = None
    duration_s: float | None = None
    final_output: str | None = field(default=None, repr=False)
    structured_output: dict[str, object] | None = field(default=None, repr=False)
    transport_attempts: tuple[RuntimeTransportAttempt, ...] = ()


class RuntimeAdapter(Protocol):
    """Transport boundary below dispatcher routing and governance policy."""

    def probe(self, request: RuntimeRequest) -> RuntimeReadiness: ...

    def run(
        self,
        request: RuntimeRequest,
        *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult: ...

    def cancel(self, handle: RuntimeHandle) -> None: ...


# Governed output schema (formerly governed_result.py).
RESULT_FIELDS = (
    "task_id",
    "status",
    "summary",
    "files_changed",
    "commit",
    "tests",
    "requirements_met",
    "risks",
    "decisions_made",
    "escalation_reason",
    "recommended_followups",
)
RESULT_STATUSES = ("completed", "blocked", "needs-escalation", "failed")
FINDING_SEVERITIES = ("critical", "important", "suggestion")
FINDING_STATES = ("open", "addressed", "waived", "stale")
MAX_RESULT_STRING = 4_000
MAX_RESULT_PATH = 1_024
MAX_RESULT_ITEMS = 64
MAX_ACTIONABLE_REVIEW_FINDINGS = 5
MAX_REVIEW_REQUIREMENTS = 5
MAX_REVIEW_LIMITATIONS = 3
MAX_RESULT_BYTES = MAX_STRUCTURED_OUTPUT_BYTES
MIN_RESULT_EXIT_CODE = -255
MAX_RESULT_EXIT_CODE = 255
MODEL_RESULT_REASON_VALUES = frozenset(reason.value for reason in ModelResultReason)
RESULT_ENFORCEMENT_VALUES = frozenset(mode.value for mode in ResultEnforcement)


def governed_result_schema(
    task_id: str, *, task: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Return the single vendor- and parent-owned worker result contract."""
    bounded_string = {"type": "string", "maxLength": MAX_RESULT_STRING}
    is_review = task is not None and task.get("work_kind") == "review"
    is_resolution = is_review and task.get("review_intent") == (
        "resolution-adjudication"
    )
    is_trust_verification = is_review and task.get("review_intent") == (
        "trust-manifest-verification"
    )

    def string_array(
        *, max_items: int = MAX_RESULT_ITEMS, pattern: str | None = None
    ) -> dict[str, object]:
        item = dict(bounded_string)
        if pattern is not None:
            item["pattern"] = pattern
        return {"type": "array", "maxItems": max_items, "items": item}

    finding_item_schema = {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": list(FINDING_SEVERITIES)},
            "claim": bounded_string,
            "path": {
                "anyOf": [
                    {"type": "string", "maxLength": MAX_RESULT_PATH},
                    {"type": "null"},
                ]
            },
            "line_start": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "null"},
                ]
            },
            "line_end": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "null"},
                ]
            },
        },
        "required": ["severity", "claim", "path", "line_start", "line_end"],
        "additionalProperties": False,
    }
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "const": task_id},
            "status": {"type": "string", "enum": list(RESULT_STATUSES)},
            "summary": bounded_string,
            "files_changed": {
                "type": "array",
                "maxItems": MAX_RESULT_ITEMS,
                "items": {"type": "string", "maxLength": MAX_RESULT_PATH},
            },
            "commit": {
                "anyOf": [
                    {"type": "string", "maxLength": 128},
                    {"type": "null"},
                ]
            },
            "tests": {
                "type": "array",
                "maxItems": MAX_RESULT_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "command": bounded_string,
                        "exit_code": {
                            "type": "integer",
                            "minimum": MIN_RESULT_EXIT_CODE,
                            "maximum": MAX_RESULT_EXIT_CODE,
                        },
                        "summary": bounded_string,
                    },
                    "required": ["command", "exit_code", "summary"],
                    "additionalProperties": False,
                },
            },
            "requirements_met": string_array(
                max_items=(MAX_REVIEW_REQUIREMENTS if is_review else MAX_RESULT_ITEMS)
            ),
            "risks": string_array(
                max_items=(
                    0
                    if is_resolution
                    else MAX_REVIEW_LIMITATIONS
                    if is_review
                    else MAX_RESULT_ITEMS
                )
            ),
            "decisions_made": string_array(
                pattern=("^(accept|narrow|merge|reject):" if is_resolution else None)
            ),
            "escalation_reason": {"anyOf": [bounded_string, {"type": "null"}]},
            "recommended_followups": string_array(
                max_items=(0 if is_review else MAX_RESULT_ITEMS)
            ),
            "findings": {
                "type": "array",
                "maxItems": 0 if is_resolution else MAX_RESULT_ITEMS,
                "items": finding_item_schema,
            },
        },
        "required": [*RESULT_FIELDS, "findings"],
        "additionalProperties": False,
    }
    if task is not None and "required_sections" in task:
        required_sections = validate_review_chain_task(task)
        properties = schema["properties"]
        required = schema["required"]
        assert isinstance(properties, dict) and isinstance(required, list)
        properties["review_sections"] = review_sections_schema(
            required_sections,
            finding_schema=finding_item_schema,
            bounded_string=bounded_string,
        )
        required.append("review_sections")
    if is_trust_verification:
        properties = schema["properties"]
        required = schema["required"]
        assert isinstance(properties, dict) and isinstance(required, list)
        properties["verification_verdict"] = {
            "type": "string",
            "enum": ["pass", "fail", "inconclusive"],
        }
        required.append("verification_verdict")
    return schema
