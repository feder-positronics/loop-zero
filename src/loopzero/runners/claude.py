"""Claude Agent SDK and same-vendor CLI runtime adapter.

The system-Python dispatcher never imports ``claude_agent_sdk``.  It starts
``sdk_bridge.py`` with the repository-managed interpreter and consumes the
small, allow-listed JSONL protocol implemented here.
"""

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import json
import math
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .contract import (
    MAX_PROTOCOL_LINE_BYTES,
    MAX_STRUCTURED_OUTPUT_BYTES,
    ReadinessFailure,
    RuntimeCostStatus,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeProgressCallback,
    RuntimeReadiness,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
    RuntimeTransportAttempt,
    RuntimeUsage,
    SubscriptionEligibility,
    TerminalReason,
)
from .process import (
    ProcessHandle,
    ProcessResult,
    cancel_cli,
    filtered_child_environment,
    isolated_python_import_available,
    merge_runtime_cache_environment,
    run_cli as default_run_cli,
)

CLAUDE_SDK_TRANSPORT = "claude/agent-sdk"
CLAUDE_CLI_TRANSPORT = "claude/cli"
CLAUDE_AUTO_TRANSPORTS = frozenset({"claude", "claude/auto"})
AUTH_STATUS_COMMAND = ["claude", "auth", "status", "--json"]
AUTH_STATUS_TIMEOUT_S = 5.0
SDK_IMPORT_TIMEOUT_S = 5.0
MAX_AUTH_STATUS_BYTES = 256 * 1024
MAX_BRIDGE_LINE_BYTES = MAX_PROTOCOL_LINE_BYTES
MAX_RETAINED_EVENTS = 256
MAX_DIAGNOSTICS = 4
MAX_METADATA_BYTES = 256
MAX_FINAL_OUTPUT_BYTES = MAX_STRUCTURED_OUTPUT_BYTES
MAX_USAGE_VALUE = 10**12
ELIGIBLE_SUBSCRIPTION_TYPES = frozenset({"enterprise", "max", "pro", "team"})
MODELS_REQUIRING_SDK_COMPATIBILITY_PROBE = frozenset({"claude-fable-5-1"})

# These names are not part of the shared process module because this adapter
# also owns the SDK subprocess environment.  They are removed before both the
# auth probe and the SDK bridge launch so a caller cannot opt into a gateway or
# metered API path through inheritance.
CLAUDE_BLOCKED_ENV_VARS = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        "CLAUDE_CODE_SKIP_MANTLE_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "AWS_BEARER_TOKEN_BEDROCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


class ClaudeProtocolError(ValueError):
    """Claude emitted malformed or incomplete normalized protocol output."""

    def __init__(self, message: str, *, semantic_event: bool = False) -> None:
        super().__init__(message)
        self.semantic_event = semantic_event


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """The only subscription facts retained from ``claude auth status``."""

    logged_in: bool
    auth_method: str
    subscription_type: str
    has_subscription: bool


@dataclass(frozen=True, slots=True)
class ParsedClaudeStream:
    """Bounded Claude metadata and terminal output.

    ``final_output`` is the execution result, not telemetry.  Keep it private
    from repr just as ``RuntimeResult`` does.
    """

    status: RuntimeStatus | None
    terminal_reason: TerminalReason | None
    session_id: str | None
    effective_model: str | None
    request_id: str | None
    usage: RuntimeUsage | None
    cost_usd: float | None
    events: tuple[RuntimeEvent, ...]
    semantic_event: bool
    diagnostics: tuple[str, ...] = ()
    final_output: str | None = field(default=None, repr=False)
    structured_output: dict[str, object] | None = field(default=None, repr=False)


def filtered_claude_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the approved native-login environment without API/gateway paths."""
    environment = filtered_child_environment(base)
    for name in CLAUDE_BLOCKED_ENV_VARS:
        environment.pop(name, None)
    return environment


def repository_root(tooling_root: Path) -> Path:
    """Normalize the injected toolchain root."""
    return get_settings().repository_root(tooling_root)


def repository_python(tooling_root: Path) -> Path:
    """Resolve the injected SDK interpreter."""
    return get_settings().interpreter(tooling_root)


def sdk_bridge_path(tooling_root: Path) -> Path:
    """Resolve the packaged or injected bridge."""
    return get_settings().bridge(tooling_root)


def build_sdk_bridge_command(worktree: Path) -> list[str]:
    return [str(repository_python(worktree)), "-I", str(sdk_bridge_path(worktree))]


def _sdk_model_readiness_payload(request: RuntimeRequest) -> str:
    """Build a content-free request that validates the governed SDK bundle."""
    return json.dumps(
        {
            "vendor": "claude-probe",
            "prompt": "",
            "cwd": str(request.cwd),
            "requested_model": request.requested_model,
            "effort": request.effort,
            "read_only": request.read_only,
            "budget_usd": None,
            "read_roots": [str(request.cwd.resolve())],
        },
        ensure_ascii=False,
    )


def parse_claude_sdk_readiness(stdout: str) -> ReadinessFailure | None:
    """Validate the content-free Claude SDK compatibility response."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > MAX_BRIDGE_LINE_BYTES:
        raise ClaudeProtocolError("Claude SDK readiness protocol was invalid")
    try:
        frame = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ClaudeProtocolError("Claude SDK readiness protocol was invalid") from exc
    if not isinstance(frame, dict) or frame.get("type") != "readiness":
        raise ClaudeProtocolError("Claude SDK readiness protocol was invalid")
    if frame == {"type": "readiness", "status": "ready"}:
        return None
    if frame != {
        "type": "readiness",
        "status": "failed",
        "failure": "model-unsupported",
    }:
        raise ClaudeProtocolError("Claude SDK readiness protocol was invalid")
    return ReadinessFailure.MODEL_UNSUPPORTED


def build_claude_command(request: RuntimeRequest) -> list[str]:
    """Build the non-interactive Claude CLI backup command."""
    command = [
        "claude",
        "-p",
        "--model",
        request.requested_model,
        "--effort",
        request.effort,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--safe-mode",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
    ]
    if request.budget_usd is not None:
        command.extend(("--max-budget-usd", f"{request.budget_usd:.2f}"))
    if request.read_only:
        if request.output_schema is None:
            raise ValueError("Claude runtime requires an output schema")
        roots = request.capability_profile.read_roots
        if len(roots) != 1 or roots[0].resolve() != request.cwd.resolve():
            raise ValueError("Claude runtime requires one governed read root")
        governed_roots = (
            roots[0].resolve(),
            *(
                root.resolve()
                for root in request.capability_profile.evidence_read_roots
            ),
        )
        scoped_rules = ",".join(
            f"{tool}({root}/**)"
            for root in governed_roots
            for tool in ("Read", "Grep", "Glob")
        )
        command.extend(
            (
                "--tools",
                "Read,Grep,Glob",
                "--allowedTools",
                scoped_rules,
                "--json-schema",
                json.dumps(request.output_schema, separators=(",", ":")),
                "--permission-mode",
                "dontAsk",
            )
        )
    else:
        raise ValueError("Claude write execution is not an approved route")
    return command


def _has_scoped_tools(request: RuntimeRequest) -> bool:
    return request.visible_tools is not None or bool(request.allowed_tools)


def _requires_sdk_transport(request: RuntimeRequest) -> bool:
    return request.requested_model in MODELS_REQUIRING_SDK_COMPATIBILITY_PROBE


# Explicit alias keeps call sites readable while allowing fixtures to use the
# transport-specific name.
build_claude_cli_command = build_claude_command


def _bounded_string(
    value: object,
    name: str,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise ClaudeProtocolError(f"Claude field {name!r} is required")
        return None
    if not isinstance(value, str) or not value:
        raise ClaudeProtocolError(f"Claude field {name!r} must be a string")
    if len(value.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ClaudeProtocolError(f"Claude field {name!r} is too long")
    return value


def parse_claude_auth_status(
    stdout: str, *, allow_validated_token: bool = False
) -> ClaudeAuthStatus:
    """Validate subscription-backed auth without retaining account identity."""
    if len(stdout.encode("utf-8")) > MAX_AUTH_STATUS_BYTES:
        raise ClaudeProtocolError("Claude auth status is too large")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeProtocolError("Claude auth status was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ClaudeProtocolError("Claude auth status must be an object")
    logged_in = payload.get("loggedIn")
    auth_method = payload.get("authMethod")
    subscription_type = payload.get("subscriptionType")
    if allow_validated_token and logged_in is True and auth_method == "oauth_token":
        return ClaudeAuthStatus(
            logged_in=True, auth_method="oauth_token",
            subscription_type="token", has_subscription=True,
        )
    if not isinstance(logged_in, bool):
        raise ClaudeProtocolError("Claude auth status loggedIn was invalid")
    if not isinstance(auth_method, str):
        raise ClaudeProtocolError("Claude auth status authMethod was invalid")
    if not isinstance(subscription_type, str) or not subscription_type.strip():
        raise ClaudeProtocolError("Claude auth status subscriptionType was invalid")
    if subscription_type.strip().lower() not in ELIGIBLE_SUBSCRIPTION_TYPES:
        raise ClaudeProtocolError("Claude auth status subscriptionType was ineligible")
    return ClaudeAuthStatus(
        logged_in=logged_in,
        auth_method=auth_method,
        subscription_type=subscription_type.strip().lower(),
        has_subscription=True,
    )


# Short name for consumers that do not need the vendor prefix.
parse_auth_status = parse_claude_auth_status


def _usage_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_USAGE_VALUE:
        return None
    return value


def _first_usage_value(
    payload: Mapping[str, object], names: Sequence[str]
) -> int | None:
    for name in names:
        parsed = _usage_integer(payload.get(name))
        if parsed is not None:
            return parsed
    return None


def _normalize_usage(
    usage: object,
    model_usage: object = None,
) -> RuntimeUsage | None:
    candidates: list[Mapping[str, object]] = []
    if isinstance(usage, dict):
        candidates.append(usage)
    if isinstance(model_usage, dict):
        if any(isinstance(value, int) for value in model_usage.values()):
            candidates.append(model_usage)
        else:
            for value in model_usage.values():
                if isinstance(value, dict):
                    candidates.append(value)
                    break
    if not candidates:
        return None
    payload = candidates[0]
    normalized = RuntimeUsage(
        input_tokens=_first_usage_value(
            payload,
            ("input_tokens", "inputTokens", "prompt_tokens"),
        ),
        output_tokens=_first_usage_value(
            payload,
            ("output_tokens", "outputTokens", "completion_tokens"),
        ),
        cache_read_tokens=_first_usage_value(
            payload,
            ("cache_read_input_tokens", "cache_read_tokens", "cacheReadInputTokens"),
        ),
        cache_write_tokens=_first_usage_value(
            payload,
            (
                "cache_creation_input_tokens",
                "cache_write_tokens",
                "cacheCreationInputTokens",
            ),
        ),
        reasoning_tokens=_first_usage_value(
            payload,
            ("reasoning_tokens", "reasoningTokens"),
        ),
    )
    if all(
        value is None
        for value in (
            normalized.input_tokens,
            normalized.output_tokens,
            normalized.cache_read_tokens,
            normalized.cache_write_tokens,
            normalized.reasoning_tokens,
        )
    ):
        return None
    return normalized


def _effective_model(raw: Mapping[str, object]) -> str | None:
    for key in ("effective_model", "model"):
        value = _bounded_string(raw.get(key), key)
        if value is not None:
            return value
    model_usage = raw.get("model_usage", raw.get("modelUsage"))
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        model_name = next(iter(model_usage))
        return _bounded_string(model_name, "model_usage")
    return None


def _append_event(
    events: list[RuntimeEvent],
    event: RuntimeEvent,
) -> None:
    if len(events) < MAX_RETAINED_EVENTS:
        events.append(event)
    elif event.kind == "result":
        events[-1] = event


def _terminal_from_frame(
    frame: Mapping[str, object],
) -> tuple[
    RuntimeStatus,
    TerminalReason,
    str | None,
    dict[str, object] | None,
    RuntimeUsage | None,
    float | None,
]:
    wire_status = frame.get("status")
    if wire_status is None:
        is_error = frame.get("is_error")
        if not isinstance(is_error, bool):
            raise ClaudeProtocolError("Claude terminal status was invalid")
        wire_status = "failed" if is_error else "completed"
    if not isinstance(wire_status, str):
        raise ClaudeProtocolError("Claude terminal status was invalid")
    if wire_status not in {RuntimeStatus.COMPLETED.value, RuntimeStatus.FAILED.value}:
        raise ClaudeProtocolError("Claude terminal status was unknown")
    status = RuntimeStatus(wire_status)

    wire_reason = frame.get("terminal_reason")
    if wire_reason is None:
        wire_reason = (
            TerminalReason.COMPLETED.value
            if status is RuntimeStatus.COMPLETED
            else TerminalReason.PROCESS_EXIT.value
        )
    if not isinstance(wire_reason, str):
        raise ClaudeProtocolError("Claude terminal reason was invalid")
    try:
        reason = TerminalReason(wire_reason)
    except ValueError as exc:
        raise ClaudeProtocolError("Claude terminal reason was unknown") from exc

    output = frame.get("output", frame.get("result"))
    if output is not None and not isinstance(output, str):
        raise ClaudeProtocolError("Claude terminal output was invalid")
    if isinstance(output, str):
        try:
            output_size = len(output.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ClaudeProtocolError(
                "Claude terminal output was not valid UTF-8"
            ) from exc
        if output_size > MAX_FINAL_OUTPUT_BYTES:
            raise ClaudeProtocolError("Claude terminal output exceeds the safe limit")
    structured_output = frame.get("structured_output")
    if structured_output is not None and not isinstance(structured_output, dict):
        raise ClaudeProtocolError("Claude structured output was invalid")
    if isinstance(structured_output, dict):
        try:
            encoded_output = json.dumps(
                structured_output,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ClaudeProtocolError(
                "Claude structured output was not valid UTF-8"
            ) from exc
        if len(encoded_output) > MAX_FINAL_OUTPUT_BYTES:
            raise ClaudeProtocolError("Claude structured output exceeds the safe limit")
    usage = _normalize_usage(
        frame.get("usage"),
        frame.get("model_usage", frame.get("modelUsage")),
    )
    raw_cost = frame.get("total_cost_usd")
    cost_usd = (
        float(raw_cost)
        if isinstance(raw_cost, (int, float))
        and not isinstance(raw_cost, bool)
        and math.isfinite(raw_cost)
        and 0 <= raw_cost <= 1_000_000
        else None
    )
    return status, reason, output, structured_output, usage, cost_usd


def parse_claude_stream(stream: str) -> ParsedClaudeStream:
    """Parse bridge JSONL or the CLI's one-object JSON output.

    Only allow-listed metadata is copied into the normalized result.  Unknown
    frames are bounded diagnostics; they cannot create a successful result.
    """
    events: list[RuntimeEvent] = []
    diagnostics: list[str] = []
    session_id: str | None = None
    effective_model: str | None = None
    request_id: str | None = None
    terminal: tuple[RuntimeStatus, TerminalReason] | None = None
    usage: RuntimeUsage | None = None
    cost_usd: float | None = None
    final_output: str | None = None
    structured_output: dict[str, object] | None = None
    semantic_seen = False
    saw_frame = False

    for line in stream.splitlines():
        if not line.strip():
            continue
        saw_frame = True
        if len(line.encode("utf-8")) > MAX_BRIDGE_LINE_BYTES:
            raise ClaudeProtocolError(
                "Claude protocol line exceeds the safe limit",
                semantic_event=semantic_seen,
            )
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClaudeProtocolError(
                "Claude emitted invalid JSON",
                semantic_event=semantic_seen,
            ) from exc
        if not isinstance(raw, dict):
            raise ClaudeProtocolError(
                "Claude protocol frames must be objects",
                semantic_event=semantic_seen,
            )
        if terminal is not None:
            raise ClaudeProtocolError(
                "Claude emitted a frame after its terminal result",
                semantic_event=True,
            )

        frame_type = raw.get("type")
        if frame_type == "event":
            kind = _bounded_string(raw.get("kind"), "kind", required=True)
            assert kind is not None
            subtype = _bounded_string(raw.get("subtype"), "subtype")
            event_semantic = raw.get(
                "semantic", kind in {"assistant", "tool_use", "tool_result"}
            )
            if not isinstance(event_semantic, bool):
                raise ClaudeProtocolError(
                    "Claude event semantic marker was invalid",
                    semantic_event=semantic_seen,
                )
            event = RuntimeEvent(kind=kind, subtype=subtype, semantic=event_semantic)
            _append_event(events, event)
            semantic_seen = semantic_seen or event.semantic
            event_session_id = _bounded_string(raw.get("session_id"), "session_id")
            if event_session_id is not None:
                session_id = event_session_id
            event_model = _bounded_string(raw.get("effective_model"), "effective_model")
            if event_model is not None:
                effective_model = event_model
            event_request_id = _bounded_string(raw.get("request_id"), "request_id")
            if event_request_id is not None:
                request_id = event_request_id
            continue

        if frame_type == "result" or (frame_type is None and "result" in raw):
            try:
                (
                    frame_status,
                    frame_reason,
                    output,
                    frame_structured_output,
                    frame_usage,
                    frame_cost,
                ) = _terminal_from_frame(raw)
            except ClaudeProtocolError as exc:
                raise ClaudeProtocolError(
                    str(exc),
                    semantic_event=True,
                ) from exc
            result_subtype = _bounded_string(raw.get("subtype"), "subtype")
            if result_subtype == "error_max_budget_usd":
                if frame_structured_output is not None:
                    frame_status = RuntimeStatus.COMPLETED
                    frame_reason = TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT
                else:
                    frame_status = RuntimeStatus.FAILED
                    frame_reason = TerminalReason.BUDGET_EXHAUSTED
            elif (
                frame_reason
                in {
                    TerminalReason.BUDGET_EXHAUSTED,
                    TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT,
                }
                or (
                    frame_status is RuntimeStatus.COMPLETED
                    and frame_reason is not TerminalReason.COMPLETED
                )
                or (
                    frame_status is RuntimeStatus.FAILED
                    and frame_reason is TerminalReason.COMPLETED
                )
            ):
                raise ClaudeProtocolError(
                    "Claude terminal status and reason were inconsistent",
                    semantic_event=True,
                )
            session_id = (
                _bounded_string(raw.get("session_id"), "session_id") or session_id
            )
            effective_model = _effective_model(raw) or effective_model
            request_id = (
                _bounded_string(raw.get("request_id"), "request_id")
                or _bounded_string(raw.get("uuid"), "uuid")
                or request_id
            )
            terminal = (frame_status, frame_reason)
            final_output = output
            structured_output = frame_structured_output
            usage = frame_usage
            cost_usd = frame_cost
            structured_output_recovery = raw.get("structured_output_recovery")
            if structured_output_recovery is not None:
                if (
                    structured_output_recovery != "accepted-tool-result"
                    or frame_status is not RuntimeStatus.COMPLETED
                    or frame_structured_output is None
                ):
                    raise ClaudeProtocolError(
                        "Claude structured-output recovery metadata was invalid",
                        semantic_event=True,
                    )
                if len(diagnostics) < MAX_DIAGNOSTICS:
                    diagnostics.append(
                        "Claude structured output recovered from accepted tool result"
                    )
            # Retain only fixed categories, never provider error text or paths.
            if (
                result_subtype
                in {
                    "error_during_execution",
                    "error_max_turns",
                    "error_max_structured_output_retries",
                }
                and len(diagnostics) < MAX_DIAGNOSTICS
            ):
                diagnostics.append(f"Claude SDK result subtype: {result_subtype}")
            denied_checks = raw.get("permission_check_denials")
            if isinstance(denied_checks, dict):
                categories = []
                for category in (
                    "missing-path",
                    "outside-root",
                    "invalid-input",
                    "unresolvable-path",
                ):
                    count = denied_checks.get(category)
                    if type(count) is int and 0 < count <= 10_000:
                        categories.append(f"{category}={count}")
                if categories and len(diagnostics) < MAX_DIAGNOSTICS:
                    diagnostics.append(
                        "Claude denied permission checks: " + ", ".join(categories)
                    )
            stop_reason = _bounded_string(raw.get("stop_reason"), "stop_reason")
            if stop_reason is not None and len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append(f"Claude stop reason: {stop_reason}")
            if (
                result_subtype == "error_max_budget_usd"
                and len(diagnostics) < MAX_DIAGNOSTICS
            ):
                recovered_after_budget = (
                    frame_reason is TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT
                )
                diagnostics.append(
                    "Claude budget exhausted after accepted structured output"
                    if recovered_after_budget
                    else "Claude budget exhausted"
                )
            api_error_status = raw.get("api_error_status")
            if (
                isinstance(api_error_status, int)
                and not isinstance(api_error_status, bool)
                and 100 <= api_error_status <= 599
                and len(diagnostics) < MAX_DIAGNOSTICS
            ):
                diagnostics.append(f"Claude API error status: {api_error_status}")
            denial_count = raw.get("permission_denial_count")
            denial_tools = raw.get("permission_denial_tools")
            if (
                isinstance(denial_count, int)
                and not isinstance(denial_count, bool)
                and 0 < denial_count <= 10_000
                and isinstance(denial_tools, list)
                and len(denial_tools) <= 16
            ):
                tools = [
                    tool
                    for value in denial_tools
                    if (tool := _bounded_string(value, "permission_denial_tool"))
                    is not None
                ]
                if len(diagnostics) < MAX_DIAGNOSTICS:
                    suffix = f": {', '.join(tools)}" if tools else ""
                    diagnostics.append(
                        f"Claude permission denials: {denial_count}{suffix}"
                    )
            _append_event(
                events,
                RuntimeEvent(
                    kind="result",
                    subtype=result_subtype,
                    semantic=True,
                ),
            )
            semantic_seen = True
            continue

        if frame_type == "error":
            error_reason = raw.get("reason")
            if not isinstance(error_reason, str) or error_reason not in {
                "startup",
                "protocol",
                "disconnect",
            }:
                raise ClaudeProtocolError(
                    "Claude error frame was unknown",
                    semantic_event=semantic_seen,
                )
            terminal = (
                RuntimeStatus.FAILED,
                {
                    "startup": TerminalReason.STARTUP_FAILURE,
                    "protocol": TerminalReason.PROTOCOL_FAILURE,
                    "disconnect": TerminalReason.TRANSPORT_DISCONNECT,
                }[error_reason],
            )
            if len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append(
                    {
                        "startup": "Claude SDK startup failed",
                        "protocol": "Claude SDK protocol failed",
                        "disconnect": "Claude SDK transport disconnected",
                    }[error_reason]
                )
            continue

        if len(diagnostics) < MAX_DIAGNOSTICS:
            diagnostics.append("Claude emitted an unknown protocol frame")

    if terminal is None:
        missing_terminal_diagnostic = (
            "Claude emitted an empty protocol stream"
            if not saw_frame
            else "Claude stream ended without a terminal result"
        )
        raise ClaudeProtocolError(
            missing_terminal_diagnostic,
            semantic_event=semantic_seen,
        )

    return ParsedClaudeStream(
        status=terminal[0],
        terminal_reason=terminal[1],
        session_id=session_id,
        effective_model=effective_model,
        request_id=request_id,
        usage=usage,
        cost_usd=cost_usd,
        events=tuple(events),
        semantic_event=semantic_seen,
        diagnostics=tuple(diagnostics[:MAX_DIAGNOSTICS]),
        final_output=final_output,
        structured_output=structured_output,
    )


parse_claude_output = parse_claude_stream


class ClaudeAdapter:
    """Claude SDK primary with prelaunch and read-only CLI fallback rules."""

    transport = CLAUDE_SDK_TRANSPORT

    def __init__(
        self,
        *,
        run_process: Callable[..., ProcessResult] = default_run_cli,
        run_cli: Callable[..., ProcessResult] | None = None,
        run_probe: Callable[..., ProcessResult] | None = None,
        sdk_available: Callable[..., bool] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._run_process = run_cli or run_process
        self._run_probe = run_probe or self._run_process
        self._sdk_available: Callable[..., bool] = (
            sdk_available or self._default_sdk_available
        )
        self._which = which
        self._settings = get_settings()

    @staticmethod
    def _default_sdk_available(python: Path, cwd: Path) -> bool:
        del cwd
        return isolated_python_import_available(
            python,
            "claude_agent_sdk",
            timeout_s=SDK_IMPORT_TIMEOUT_S,
            env=filtered_claude_environment(),
        )

    @staticmethod
    def _runtime_write_root(request: RuntimeRequest) -> Path:
        """Return the one root made writable by the outer worker sandbox."""
        write_roots = request.capability_profile.write_roots
        if not write_roots:
            return request.cwd
        if len(write_roots) != 1:
            raise ClaudeProtocolError(
                "Claude runtime requires exactly one outer writable root"
            )
        return write_roots[0]

    def _auth_readiness(self, request: RuntimeRequest) -> RuntimeReadiness:
        if request.eligibility is not SubscriptionEligibility.APPROVED:
            return RuntimeReadiness(
                ready=False,
                eligibility=request.eligibility,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="confirm the approved Claude subscription login path",
            )
        if self._which("claude") is None:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.EXECUTABLE_MISSING,
                repair="install or restore the approved Claude CLI",
            )
        try:
            outcome = self._run_probe(
                AUTH_STATUS_COMMAND,
                cwd=request.cwd,
                input_text="",
                timeout_s=AUTH_STATUS_TIMEOUT_S,
                env=filtered_claude_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="run claude auth login for the approved subscription",
            )
        if outcome.timed_out or outcome.returncode != 0:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="run claude auth login for the approved subscription",
            )
        try:
            from .claude_token import protected_token_available

            auth = parse_claude_auth_status(
                outcome.stdout, allow_validated_token=protected_token_available()
            )
        except ClaudeProtocolError:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.AMBIGUOUS,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="confirm Claude auth status reports a claude.ai subscription",
            )
        if (
            not auth.logged_in
            or auth.auth_method not in {"claude.ai", "oauth_token"}
            or not auth.has_subscription
        ):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="run claude auth login for an eligible Claude subscription",
            )
        return RuntimeReadiness(
            ready=True,
            eligibility=SubscriptionEligibility.APPROVED,
        )

    @using_adapter_settings
    def probe_cli(self, request: RuntimeRequest) -> RuntimeReadiness:
        if _requires_sdk_transport(request):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.MODEL_UNSUPPORTED,
                repair="use the governed Claude SDK transport for this model",
                transport=CLAUDE_CLI_TRANSPORT,
            )
        return self._auth_readiness(request)

    @using_adapter_settings
    def probe_sdk(self, request: RuntimeRequest) -> RuntimeReadiness:
        python = repository_python(request.tooling_root or request.cwd)
        if os.name != "posix":
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.UNSUPPORTED_PLATFORM,
                repair="use a supported POSIX runtime environment for Claude SDK work",
            )
        sdk_ready = self._sdk_available(python, request.cwd)
        if not sdk_ready:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.SDK_UNAVAILABLE,
                repair="restore the repository Claude Agent SDK tooling dependency",
            )
        if request.requested_model in MODELS_REQUIRING_SDK_COMPATIBILITY_PROBE:
            try:
                outcome = self._run_probe(
                    build_sdk_bridge_command(request.tooling_root or request.cwd),
                    cwd=request.cwd,
                    input_text=_sdk_model_readiness_payload(request),
                    timeout_s=min(SDK_IMPORT_TIMEOUT_S, request.timeout_s),
                    env=filtered_claude_environment(),
                )
            except (OSError, subprocess.TimeoutExpired):
                outcome = None
            if outcome is None or outcome.timed_out or outcome.output_limited:
                return RuntimeReadiness(
                    ready=False,
                    eligibility=SubscriptionEligibility.UNAVAILABLE,
                    failure=ReadinessFailure.PROTOCOL_INCOMPATIBLE,
                    repair="repair the governed Claude SDK compatibility probe",
                    transport=CLAUDE_SDK_TRANSPORT,
                )
            try:
                failure = parse_claude_sdk_readiness(outcome.stdout)
            except ClaudeProtocolError:
                return RuntimeReadiness(
                    ready=False,
                    eligibility=SubscriptionEligibility.AMBIGUOUS,
                    failure=ReadinessFailure.PROTOCOL_INCOMPATIBLE,
                    repair="repair the governed Claude SDK compatibility probe",
                    transport=CLAUDE_SDK_TRANSPORT,
                )
            if failure is not None:
                return RuntimeReadiness(
                    ready=False,
                    eligibility=SubscriptionEligibility.UNAVAILABLE,
                    failure=failure,
                    repair=(
                        "upgrade the governed Claude SDK runtime for the "
                        "configured model"
                    ),
                    transport=CLAUDE_SDK_TRANSPORT,
                )
            if outcome.returncode != 0:
                return RuntimeReadiness(
                    ready=False,
                    eligibility=SubscriptionEligibility.AMBIGUOUS,
                    failure=ReadinessFailure.PROTOCOL_INCOMPATIBLE,
                    repair="repair the governed Claude SDK compatibility probe",
                    transport=CLAUDE_SDK_TRANSPORT,
                )
        return self._auth_readiness(request)

    @using_adapter_settings
    def select_transport(
        self,
        request: RuntimeRequest,
    ) -> tuple[RuntimeRequest | None, RuntimeReadiness]:
        """Select SDK or CLI before launching either transport."""
        sdk_readiness = self.probe_sdk(request)
        if sdk_readiness.ready:
            return replace(request, transport=CLAUDE_SDK_TRANSPORT), sdk_readiness
        if (
            _requires_sdk_transport(request)
            or sdk_readiness.failure is ReadinessFailure.MODEL_UNSUPPORTED
            or _has_scoped_tools(request)
        ):
            return None, sdk_readiness
        cli_readiness = self.probe_cli(request)
        if cli_readiness.ready:
            return replace(request, transport=CLAUDE_CLI_TRANSPORT), cli_readiness
        return None, RuntimeReadiness(
            ready=False,
            eligibility=cli_readiness.eligibility,
            failure=cli_readiness.failure or sdk_readiness.failure,
            repair=cli_readiness.repair or sdk_readiness.repair,
        )

    @using_adapter_settings
    def probe(self, request: RuntimeRequest) -> RuntimeReadiness:
        if request.transport == CLAUDE_CLI_TRANSPORT:
            return self.probe_cli(request)
        if request.transport in CLAUDE_AUTO_TRANSPORTS:
            _, readiness = self.select_transport(request)
            return readiness
        return self.probe_sdk(request)

    @using_adapter_settings
    def cancel(self, handle: RuntimeHandle | ProcessHandle) -> None:
        if not isinstance(handle, ProcessHandle):
            raise TypeError("Claude cancellation requires an owned process handle")
        cancel_cli(handle)

    @using_adapter_settings
    def run(
        self,
        request: RuntimeRequest,
        *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult:
        preferred_attempt: RuntimeTransportAttempt | None = None
        if request.transport in {*CLAUDE_AUTO_TRANSPORTS, CLAUDE_SDK_TRANSPORT}:
            sdk_readiness = self.probe_sdk(request)
            if sdk_readiness.ready:
                request = replace(request, transport=CLAUDE_SDK_TRANSPORT)
            else:
                if (
                    _requires_sdk_transport(request)
                    or sdk_readiness.failure is ReadinessFailure.MODEL_UNSUPPORTED
                    or _has_scoped_tools(request)
                ):
                    return self._unavailable(request, sdk_readiness)
                fallback_request = replace(
                    request,
                    transport=CLAUDE_CLI_TRANSPORT,
                )
                cli_readiness = self.probe_cli(fallback_request)
                preferred_attempt = self._readiness_attempt(
                    request,
                    sdk_readiness,
                    selected_next=cli_readiness.ready,
                )
                if not cli_readiness.ready:
                    unavailable = self._unavailable(fallback_request, cli_readiness)
                    return replace(
                        unavailable,
                        transport_attempts=(
                            preferred_attempt,
                            *unavailable.transport_attempts,
                        ),
                    )
                request = fallback_request
        if request.transport == CLAUDE_CLI_TRANSPORT:
            if _requires_sdk_transport(request):
                return self._unavailable(request, self.probe_cli(request))
            if _has_scoped_tools(request):
                return self._failure(
                    request,
                    status=RuntimeStatus.FAILED,
                    reason=TerminalReason.PROTOCOL_FAILURE,
                    diagnostics=("scoped Claude tools require the SDK transport",),
                )
            readiness = self.probe_cli(request)
            if not readiness.ready:
                return self._unavailable(request, readiness)
            result = self._run_cli(request)
            if preferred_attempt is not None:
                return replace(
                    result,
                    transport_attempts=(
                        preferred_attempt,
                        *result.transport_attempts,
                    ),
                )
            return result
        if request.transport != CLAUDE_SDK_TRANSPORT:
            return self._unavailable(request, self.probe(request))
        return self._run_sdk(request, on_progress=on_progress)

    def _run_cli(self, request: RuntimeRequest) -> RuntimeResult:
        environment = merge_runtime_cache_environment(
            filtered_claude_environment(),
            request.capability_profile.environment,
            write_root=self._runtime_write_root(request),
        )
        try:
            outcome = self._run_process(
                build_claude_command(request),
                cwd=request.cwd,
                input_text=request.prompt,
                timeout_s=request.timeout_s,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._failure(
                request,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.STARTUP_FAILURE,
                diagnostics=("Claude CLI startup failed",),
            )
        if outcome.timed_out:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                diagnostics=("Claude CLI timed out",),
            )
        if outcome.output_limited:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Claude CLI output exceeded the safe limit",),
            )
        try:
            parsed = parse_claude_stream(outcome.stdout)
        except ClaudeProtocolError as exc:
            reason = (
                TerminalReason.PROTOCOL_FAILURE
                if exc.semantic_event or outcome.returncode == 0
                else TerminalReason.STARTUP_FAILURE
            )
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=reason,
                diagnostics=("Claude CLI emitted malformed output",),
            )
        if parsed.status is None or parsed.terminal_reason is None:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Claude CLI emitted no terminal result",),
            )
        status = parsed.status
        reason = parsed.terminal_reason
        diagnostics = parsed.diagnostics
        if (
            outcome.returncode != 0
            and status is RuntimeStatus.COMPLETED
            and reason is not TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT
        ):
            status = RuntimeStatus.FAILED
            reason = TerminalReason.PROCESS_EXIT
            diagnostics = ("Claude CLI exited unsuccessfully",)
        return self._result(
            request,
            outcome=outcome,
            status=status,
            reason=reason,
            session_id=parsed.session_id,
            effective_model=parsed.effective_model,
            request_id=parsed.request_id,
            usage=parsed.usage,
            cost_usd=parsed.cost_usd,
            events=parsed.events,
            diagnostics=diagnostics,
            final_output=parsed.final_output,
            structured_output=parsed.structured_output,
        )

    def _run_sdk(
        self,
        request: RuntimeRequest,
        *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult:
        payload_data: dict[str, object] = {
            "prompt": request.prompt,
            "cwd": str(request.cwd),
            "requested_model": request.requested_model,
            "effort": request.effort,
            "read_only": request.read_only,
            "budget_usd": request.budget_usd,
            "output_schema": request.output_schema,
            "read_roots": [
                str(root.resolve()) for root in request.capability_profile.read_roots
            ],
            "evidence_read_roots": [
                str(root.resolve())
                for root in request.capability_profile.evidence_read_roots
            ],
            "commercial_mode": request.commercial_mode.value,
        }
        if request.visible_tools is not None:
            payload_data["visible_tools"] = list(request.visible_tools)
        if request.allowed_tools:
            payload_data["allowed_tools"] = list(request.allowed_tools)
        payload = json.dumps(payload_data, ensure_ascii=False)
        try:
            environment = merge_runtime_cache_environment(
                filtered_claude_environment(),
                request.capability_profile.environment,
                write_root=self._runtime_write_root(request),
            )
            outcome = self._run_process(
                build_sdk_bridge_command(request.tooling_root or request.cwd),
                cwd=request.cwd,
                input_text=payload,
                timeout_s=request.timeout_s,
                env=environment,
                on_progress=on_progress,
            )
        except subprocess.TimeoutExpired:
            return self._failure(
                request,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                diagnostics=("Claude SDK bridge timed out",),
            )
        except OSError:
            if request.read_only and not _has_scoped_tools(request):
                return self._run_read_only_fallback(
                    request, reason=TerminalReason.STARTUP_FAILURE
                )
            return self._failure(
                request,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.STARTUP_FAILURE,
                diagnostics=("Claude SDK bridge startup failed",),
            )
        if outcome.timed_out:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                diagnostics=("Claude SDK bridge timed out",),
            )
        if outcome.output_limited:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Claude SDK bridge output exceeded the safe limit",),
            )
        try:
            parsed = parse_claude_stream(outcome.stdout)
        except ClaudeProtocolError as exc:
            if (
                request.read_only
                and not _has_scoped_tools(request)
                and not exc.semantic_event
            ):
                return self._run_read_only_fallback(
                    request,
                    reason=TerminalReason.PROTOCOL_FAILURE,
                    duration_s=outcome.duration_s,
                    progress_diagnostic=outcome.progress_diagnostic,
                )
            reason = (
                TerminalReason.PROTOCOL_FAILURE
                if exc.semantic_event or outcome.returncode == 0
                else TerminalReason.STARTUP_FAILURE
            )
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=reason,
                diagnostics=("Claude SDK bridge emitted malformed protocol",),
            )
        if parsed.status is None or parsed.terminal_reason is None:
            if (
                request.read_only
                and not _has_scoped_tools(request)
                and not parsed.semantic_event
            ):
                return self._run_read_only_fallback(
                    request,
                    reason=TerminalReason.PROTOCOL_FAILURE,
                    duration_s=outcome.duration_s,
                    progress_diagnostic=outcome.progress_diagnostic,
                )
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Claude SDK bridge emitted no terminal result",),
            )
        if (
            request.read_only
            and not _has_scoped_tools(request)
            and not parsed.semantic_event
            and parsed.terminal_reason
            in {
                TerminalReason.STARTUP_FAILURE,
                TerminalReason.PROTOCOL_FAILURE,
                TerminalReason.TRANSPORT_DISCONNECT,
            }
        ):
            return self._run_read_only_fallback(
                request,
                reason=parsed.terminal_reason,
                duration_s=outcome.duration_s,
                progress_diagnostic=outcome.progress_diagnostic,
            )
        status = parsed.status
        reason = parsed.terminal_reason
        diagnostics = parsed.diagnostics
        if (
            outcome.returncode != 0
            and status is RuntimeStatus.COMPLETED
            and reason is not TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT
        ):
            status = RuntimeStatus.FAILED
            reason = TerminalReason.PROCESS_EXIT
            diagnostics = ("Claude SDK bridge exited unsuccessfully",)
        return self._result(
            request,
            outcome=outcome,
            status=status,
            reason=reason,
            session_id=parsed.session_id,
            effective_model=parsed.effective_model,
            request_id=parsed.request_id,
            usage=parsed.usage,
            cost_usd=parsed.cost_usd,
            events=parsed.events,
            diagnostics=diagnostics,
            final_output=parsed.final_output,
            structured_output=parsed.structured_output,
        )

    def _run_read_only_fallback(
        self,
        request: RuntimeRequest,
        *,
        reason: TerminalReason,
        duration_s: float | None = None,
        progress_diagnostic: bool = False,
    ) -> RuntimeResult:
        def retain_progress_diagnostic(result: RuntimeResult) -> RuntimeResult:
            if (
                not progress_diagnostic
                or "Native runtime progress was dropped" in result.diagnostics
                or len(result.diagnostics) >= MAX_DIAGNOSTICS
            ):
                return result
            return replace(
                result,
                diagnostics=(
                    *result.diagnostics,
                    "Native runtime progress was dropped",
                ),
            )

        if _requires_sdk_transport(request):
            return self._failure(
                request,
                status=RuntimeStatus.FAILED,
                reason=reason,
                diagnostics=(
                    "configured Claude model requires the governed SDK transport",
                ),
            )

        if _has_scoped_tools(request):
            return self._failure(
                request,
                status=RuntimeStatus.FAILED,
                reason=reason,
                diagnostics=("scoped Claude tools require the SDK transport",),
            )
        fallback_request = replace(
            request,
            transport=CLAUDE_CLI_TRANSPORT,
        )
        readiness = self.probe_cli(fallback_request)
        preferred = RuntimeTransportAttempt(
            transport=request.transport,
            requested_model=request.requested_model,
            phase="run",
            status=RuntimeStatus.FAILED.value,
            terminal_reason=reason.value,
            failure_class=(
                "startup" if reason is TerminalReason.STARTUP_FAILURE else "protocol"
            ),
            duration_s=duration_s,
            semantic=False,
            selected_next=readiness.ready,
        )
        if not readiness.ready:
            unavailable = self._unavailable(fallback_request, readiness)
            return retain_progress_diagnostic(
                replace(
                    unavailable,
                    transport_attempts=(preferred, *unavailable.transport_attempts),
                )
            )
        result = self._run_cli(fallback_request)
        return retain_progress_diagnostic(
            replace(
                result,
                transport_attempts=(preferred, *result.transport_attempts),
            )
        )

    @staticmethod
    def _readiness_attempt(
        request: RuntimeRequest,
        readiness: RuntimeReadiness,
        *,
        selected_next: bool,
    ) -> RuntimeTransportAttempt:
        return RuntimeTransportAttempt(
            transport=CLAUDE_SDK_TRANSPORT,
            requested_model=request.requested_model,
            phase="readiness",
            status=RuntimeStatus.SUBSCRIPTION_UNAVAILABLE.value,
            terminal_reason=TerminalReason.SUBSCRIPTION_UNAVAILABLE.value,
            failure_class=(readiness.failure.value if readiness.failure else None),
            duration_s=None,
            semantic=False,
            selected_next=selected_next,
        )

    def _unavailable(
        self,
        request: RuntimeRequest,
        readiness: RuntimeReadiness,
    ) -> RuntimeResult:
        diagnostic = readiness.repair or "Claude subscription transport unavailable"
        return RuntimeResult(
            vendor="claude",
            transport=request.transport,
            requested_model=request.requested_model,
            status=RuntimeStatus.SUBSCRIPTION_UNAVAILABLE,
            terminal_reason=TerminalReason.SUBSCRIPTION_UNAVAILABLE,
            attempt_id=request.attempt_id,
            eligibility=readiness.eligibility,
            commercial_mode=request.commercial_mode,
            fallback_from=request.fallback_from,
            diagnostics=(diagnostic,),
            transport_attempts=(
                RuntimeTransportAttempt(
                    transport=request.transport,
                    requested_model=request.requested_model,
                    phase="readiness",
                    status=RuntimeStatus.SUBSCRIPTION_UNAVAILABLE.value,
                    terminal_reason=TerminalReason.SUBSCRIPTION_UNAVAILABLE.value,
                    failure_class=(
                        readiness.failure.value
                        if readiness.failure is not None
                        else "eligibility"
                    ),
                    duration_s=None,
                    semantic=False,
                    selected_next=False,
                ),
            ),
        )

    def _failure(
        self,
        request: RuntimeRequest,
        *,
        status: RuntimeStatus,
        reason: TerminalReason,
        diagnostics: tuple[str, ...],
    ) -> RuntimeResult:
        return RuntimeResult(
            vendor="claude",
            transport=request.transport,
            requested_model=request.requested_model,
            status=status,
            terminal_reason=reason,
            attempt_id=request.attempt_id,
            eligibility=request.eligibility,
            commercial_mode=request.commercial_mode,
            fallback_from=request.fallback_from,
            diagnostics=diagnostics[:MAX_DIAGNOSTICS],
            transport_attempts=(
                RuntimeTransportAttempt(
                    transport=request.transport,
                    requested_model=request.requested_model,
                    phase="run",
                    status=status.value,
                    terminal_reason=reason.value,
                    failure_class=reason.value,
                    duration_s=None,
                    semantic=False,
                    selected_next=False,
                ),
            ),
        )

    def _result(
        self,
        request: RuntimeRequest,
        *,
        outcome: ProcessResult | None = None,
        status: RuntimeStatus,
        reason: TerminalReason,
        session_id: str | None = None,
        effective_model: str | None = None,
        request_id: str | None = None,
        usage: RuntimeUsage | None = None,
        cost_usd: float | None = None,
        events: tuple[RuntimeEvent, ...] = (),
        diagnostics: tuple[str, ...] = (),
        final_output: str | None = None,
        structured_output: dict[str, object] | None = None,
    ) -> RuntimeResult:
        bounded_diagnostics = list(diagnostics[:MAX_DIAGNOSTICS])
        if (
            outcome is not None
            and outcome.progress_diagnostic
            and len(bounded_diagnostics) < MAX_DIAGNOSTICS
        ):
            bounded_diagnostics.append("Native runtime progress was dropped")
        return RuntimeResult(
            vendor="claude",
            transport=request.transport,
            requested_model=request.requested_model,
            status=status,
            terminal_reason=reason,
            attempt_id=request.attempt_id,
            effective_model=effective_model,
            session_id=session_id,
            request_id=request_id,
            usage=usage,
            cost_usd=cost_usd,
            cost_status=(
                RuntimeCostStatus.ESTIMATED
                if cost_usd is not None
                else RuntimeCostStatus.UNKNOWN
            ),
            eligibility=request.eligibility,
            commercial_mode=request.commercial_mode,
            fallback_from=request.fallback_from,
            events=events,
            diagnostics=tuple(bounded_diagnostics),
            returncode=outcome.returncode if outcome is not None else None,
            duration_s=outcome.duration_s if outcome is not None else None,
            final_output=final_output,
            structured_output=structured_output,
            transport_attempts=(
                RuntimeTransportAttempt(
                    transport=request.transport,
                    requested_model=request.requested_model,
                    phase="run",
                    status=status.value,
                    terminal_reason=reason.value,
                    failure_class=(
                        None if status is RuntimeStatus.COMPLETED else reason.value
                    ),
                    duration_s=(outcome.duration_s if outcome is not None else None),
                    semantic=any(event.semantic for event in events),
                    selected_next=False,
                ),
            ),
        )
