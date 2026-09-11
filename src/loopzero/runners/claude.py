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
    is_valid_resume_session_id,
)
from .process import (
    ProcessHandle,
    ProcessResult,
    SandboxWrapper,
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
    if request.resume_session_id is not None and not is_valid_resume_session_id(
        request.resume_session_id
    ):
        raise ValueError("Claude resume_session_id is invalid")
    command = [
        "claude",
        "-p",
        "--model",
        request.requested_model,
        "--effort",
        request.effort,
        "--output-format",
        "json",
    ]
    if request.resume_session_id is None:
        command.append("--no-session-persistence")
    else:
        command.extend(("--resume", request.resume_session_id))
    command.extend([
        "--safe-mode",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
    ])
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
                    env={**filtered_claude_environment(), **get_settings().child_environment()},
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
        if request.resume_session_id is not None:
            payload_data["resume_session_id"] = request.resume_session_id
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
                env={**environment, **get_settings().child_environment()},
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


# Former claude_credential.py: names and call signatures are preserved.

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import fcntl
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path


MAX_CREDENTIAL_BYTES = 1024 * 1024
REFRESH_TIMEOUT_S = 30.0
REFRESH_SAFETY_MARGIN_S = 5 * 60
MAX_REFRESH_ATTEMPTS = 2
BROKER_LOCK_NAME = ".oauth_refresh.lock"
CLAUDE_AUTH_FD_ENV = DEFAULT_SETTINGS.env_name("CLAUDE_AUTH_FD")
CLAUDE_AUTH_STATE_ENV = DEFAULT_SETTINGS.env_name("CLAUDE_AUTH_STATE")
REQUIRED_SCOPES = frozenset(
    {
        "user:inference",
        "user:profile",
        "user:sessions:claude_code",
    }
)


class ClaudeCredentialError(RuntimeError):
    """The host broker could not produce a safe Claude credential snapshot."""


class UnsafeClaudeCredential(ClaudeCredentialError):
    """A credential or broker-owned file violated the safety contract."""


class ClaudeCredentialUnavailable(ClaudeCredentialError):
    """The protected Claude credential or runtime is unavailable."""


class ClaudeCredentialRefreshTimeout(ClaudeCredentialError):
    """Claude did not finish the isolated OAuth refresh within its deadline."""


class ClaudeCredentialRevoked(ClaudeCredentialError):
    """Claude explicitly reported that the subscription login is revoked."""


class ClaudeCredentialRefreshFailed(ClaudeCredentialError):
    """Claude could not refresh the credential, without proof of revocation."""


@dataclass(frozen=True, slots=True)
class _ValidatedOAuth:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    refresh_expires_at_ms: int
    scopes: frozenset[str]
    subscription_type: str


@dataclass(frozen=True, slots=True)
class _ValidatedCredential:
    payload: bytes
    decoded: dict[str, object]
    oauth: _ValidatedOAuth

    @property
    def expires_at_ms(self) -> int:
        return self.oauth.expires_at_ms


def _integer_milliseconds(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsafeClaudeCredential(f"Claude credential {name} is invalid")
    parsed = int(value)
    if parsed != value or parsed <= 0:
        raise UnsafeClaudeCredential(f"Claude credential {name} is invalid")
    return parsed


def _validate_payload(payload: bytes, *, now_ms: int) -> _ValidatedCredential:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeClaudeCredential("Claude credential JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise UnsafeClaudeCredential("Claude credential JSON is invalid")
    oauth = decoded.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise UnsafeClaudeCredential("Claude OAuth credential is missing")
    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise UnsafeClaudeCredential("Claude credential accessToken is invalid")
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise UnsafeClaudeCredential("Claude credential refreshToken is invalid")
    expires_at_ms = _integer_milliseconds(oauth.get("expiresAt"), name="expiresAt")
    refresh_expires_at_ms = _integer_milliseconds(
        oauth.get("refreshTokenExpiresAt"), name="refreshTokenExpiresAt"
    )
    if refresh_expires_at_ms <= now_ms:
        raise ClaudeCredentialRevoked("Claude refresh credential has expired")
    raw_scopes = oauth.get("scopes")
    if (
        not isinstance(raw_scopes, list)
        or any(not isinstance(scope, str) or not scope for scope in raw_scopes)
        or not REQUIRED_SCOPES.issubset(raw_scopes)
    ):
        raise UnsafeClaudeCredential("Claude credential scopes are invalid")
    subscription_type = oauth.get("subscriptionType")
    if (
        not isinstance(subscription_type, str)
        or subscription_type.strip().lower() not in ELIGIBLE_SUBSCRIPTION_TYPES
    ):
        raise UnsafeClaudeCredential("Claude subscription type is invalid")
    return _ValidatedCredential(
        payload=payload,
        decoded=decoded,
        oauth=_ValidatedOAuth(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
            refresh_expires_at_ms=refresh_expires_at_ms,
            scopes=frozenset(raw_scopes),
            subscription_type=subscription_type.strip().lower(),
        ),
    )


def _read_private_payload(
    path: Path, *, directory_fd: int | None = None, single_link: bool = False
) -> bytes:
    if directory_fd is None and path.is_symlink():
        raise UnsafeClaudeCredential("Claude credential is unsafe")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ClaudeCredentialUnavailable("Claude credential is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (single_link and metadata.st_nlink != 1)
            or metadata.st_size <= 2
            or metadata.st_size > MAX_CREDENTIAL_BYTES
        ):
            raise UnsafeClaudeCredential("Claude credential is unsafe")
        payload = os.pread(descriptor, metadata.st_size, 0)
        if len(payload) != metadata.st_size:
            raise UnsafeClaudeCredential("Claude credential read was incomplete")
    finally:
        os.close(descriptor)
    return payload


def _read_credential(path: Path, *, now_ms: int) -> _ValidatedCredential:
    return _validate_payload(_read_private_payload(path), now_ms=now_ms)


def _sandbox_snapshot_payload(credential: _ValidatedCredential) -> bytes:
    """Expose only validated fields and replace the durable refresh capability."""
    oauth = credential.oauth
    snapshot = {
        "claudeAiOauth": {
            "accessToken": oauth.access_token,
            "refreshToken": oauth.access_token,
            "expiresAt": oauth.expires_at_ms,
            "refreshTokenExpiresAt": oauth.expires_at_ms,
            "scopes": sorted(oauth.scopes),
            "subscriptionType": oauth.subscription_type,
        }
    }
    return json.dumps(snapshot, separators=(",", ":")).encode("utf-8")


@contextmanager
def _renewal_lock(path: Path) -> Iterator[None]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UnsafeClaudeCredential("Claude renewal lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise UnsafeClaudeCredential("Claude renewal lock is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise UnsafeClaudeCredential("Claude renewal lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _staged_refresh_environment(home: Path) -> dict[str, str]:
    environment = filtered_claude_environment()
    for name in tuple(environment):
        if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_")) or name in {
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        }:
            environment.pop(name, None)
    environment["HOME"] = str(home)
    return environment


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _force_staged_expiry(credential: _ValidatedCredential, *, now_ms: int) -> bytes:
    decoded = json.loads(json.dumps(credential.decoded))
    oauth = decoded["claudeAiOauth"]
    assert isinstance(oauth, dict)
    oauth["expiresAt"] = max(1, now_ms - 1)
    return json.dumps(decoded, separators=(",", ":")).encode("utf-8")


def _status_is_revoked(stdout: str) -> bool:
    try:
        status = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(status, dict) and status.get("loggedIn") is False


def _run_refresh_process_group(
    command: Sequence[str],
    *,
    timeout: float,
    check: bool,
    capture_output: bool,
    text: bool,
    env: dict[str, str],
    cwd: Path,
    sandbox_wrapper: SandboxWrapper | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Claude refresh through the mandatory contained process seam."""
    if check or not capture_output or not text:
        raise ValueError("Claude refresh runner contract is invalid")
    result = default_run_cli(
        command,
        cwd=cwd,
        input_text="",
        timeout_s=timeout,
        env=env,
        sandbox_wrapper=sandbox_wrapper,
    )
    if result.timed_out:
        raise subprocess.TimeoutExpired(
            command, timeout, output=result.stdout, stderr=result.stderr
        )
    return subprocess.CompletedProcess(
        command, result.returncode, result.stdout, result.stderr
    )


def _refresh_credential(
    credential: _ValidatedCredential,
    *,
    now_ms: int,
    horizon_ms: int,
    claude_binary: Path,
    run_status: Callable[..., subprocess.CompletedProcess[str]],
    sandbox_wrapper: SandboxWrapper | None = None,
) -> _ValidatedCredential:
    settings = get_settings()
    tooling_root = settings.tooling_root or Path.cwd()
    workspace_root = settings.workspace_root(tooling_root)
    workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=settings.temp_name("claude-renewal"), dir=workspace_root
    ) as directory:
        staging_home = Path(directory)
        staging_home.chmod(0o700)
        staging_directory = staging_home / ".claude"
        staging_directory.mkdir(mode=0o700)
        staging_credential = staging_directory / ".credentials.json"
        _write_private_file(
            staging_credential,
            _force_staged_expiry(credential, now_ms=now_ms),
        )
        command = [str(claude_binary), "auth", "status", "--json"]
        try:
            runner_arguments = {
                "timeout": REFRESH_TIMEOUT_S,
                "check": False,
                "capture_output": True,
                "text": True,
                "env": _staged_refresh_environment(staging_home),
                "cwd": staging_home,
            }
            if run_status is _run_refresh_process_group:
                runner_arguments["sandbox_wrapper"] = sandbox_wrapper
            outcome = run_status(command, **runner_arguments)
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCredentialRefreshTimeout(
                "Claude credential refresh timed out"
            ) from exc
        except OSError as exc:
            raise ClaudeCredentialRefreshFailed(
                "Claude credential refresh could not start"
            ) from exc
        if _status_is_revoked(outcome.stdout):
            raise ClaudeCredentialRevoked("Claude subscription login was revoked")
        if outcome.returncode != 0:
            raise ClaudeCredentialRefreshFailed("Claude credential refresh failed")
        refreshed = _read_credential(staging_credential, now_ms=now_ms)
        if (
            refreshed.expires_at_ms <= credential.expires_at_ms
            or refreshed.expires_at_ms <= horizon_ms
        ):
            raise ClaudeCredentialRefreshFailed(
                "Claude credential refresh did not advance expiry"
            )
        return refreshed


def _install_credential(path: Path, credential: _ValidatedCredential) -> None:
    parent = path.parent
    try:
        parent_metadata = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise UnsafeClaudeCredential(
            "Claude credential directory is unavailable"
        ) from exc
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o002
    ):
        raise UnsafeClaudeCredential("Claude credential directory is unsafe")
    temporary = parent / f".credentials-{uuid.uuid4().hex}.tmp"
    try:
        _write_private_file(temporary, credential.payload)
        temporary.replace(path)
        path.chmod(0o600, follow_symlinks=False)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("Claude credential snapshot write was incomplete")
        written += count


def _snapshot_descriptor(payload: bytes) -> int:
    if hasattr(os, "memfd_create"):
        descriptor = os.memfd_create(
            get_settings().memfd_name("claude-auth"),
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        try:
            _write_all(descriptor, payload)
            os.fchmod(descriptor, 0o400)
            fcntl.fcntl(
                descriptor,
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_SEAL,
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    writable, raw_path = tempfile.mkstemp(prefix=get_settings().temp_name("claude-auth"))
    path = Path(raw_path)
    try:
        _write_all(writable, payload)
        os.fchmod(writable, 0o400)
        readonly = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    finally:
        os.close(writable)
        path.unlink(missing_ok=True)
    return readonly


def _resolve_claude_binary() -> Path:
    binary = shutil.which("claude")
    if binary is None:
        raise ClaudeCredentialUnavailable("Claude CLI is unavailable")
    resolved = Path(binary).resolve(strict=False)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ClaudeCredentialUnavailable("Claude CLI is unavailable")
    return resolved


@contextmanager
def _oauth_subscription_credential(
    *,
    requested_runtime_s: float,
    credential_path: Path | None = None,
    clock: Callable[[], float] = time.time,
    run_status: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    claude_binary: Path | None = None,
    sandbox_wrapper: SandboxWrapper | None = None,
) -> Iterator[int]:
    """Renew if needed, then lend one sealed snapshot for a provider command."""
    if requested_runtime_s <= 0:
        raise ValueError("requested_runtime_s must be positive")
    path = credential_path or Path.home() / ".claude" / ".credentials.json"
    with _renewal_lock(path.parent / BROKER_LOCK_NAME):
        now_ms = int(clock() * 1000)
        horizon_ms = now_ms + int(
            (requested_runtime_s + REFRESH_SAFETY_MARGIN_S) * 1000
        )
        credential = _read_credential(path, now_ms=now_ms)
        refresh_attempts = 0
        refresh_binary = claude_binary
        while credential.expires_at_ms <= horizon_ms:
            if refresh_attempts >= MAX_REFRESH_ATTEMPTS:
                raise ClaudeCredentialRefreshFailed(
                    "Claude credential changed during trusted refresh"
                )
            refresh_attempts += 1
            if refresh_binary is None:
                refresh_binary = _resolve_claude_binary()
            refreshed = _refresh_credential(
                credential,
                now_ms=now_ms,
                horizon_ms=horizon_ms,
                claude_binary=refresh_binary,
                run_status=run_status or _run_refresh_process_group,
                sandbox_wrapper=sandbox_wrapper,
            )
            current = _read_credential(path, now_ms=now_ms)
            if current.oauth != credential.oauth:
                credential = current
                continue
            # Claude login does not expose an atomic compare/exchange API to this
            # broker. Replacements visible here win; a later external replace is
            # necessarily governed by the filesystem's last-writer-wins order.
            _install_credential(path, refreshed)
            installed = _read_credential(path, now_ms=now_ms)
            if installed.oauth != refreshed.oauth:
                raise ClaudeCredentialRefreshFailed(
                    "Claude credential refresh race during trusted installation"
                )
            credential = installed
        snapshot = _snapshot_descriptor(_sandbox_snapshot_payload(credential))
    try:
        yield snapshot
    finally:
        os.close(snapshot)


@contextmanager
def claude_subscription_credential(
    *,
    requested_runtime_s: float,
    credential_path: Path | None = None,
    clock: Callable[[], float] = time.time,
    run_status: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    claude_binary: Path | None = None,
    sandbox_wrapper: SandboxWrapper | None = None,
) -> Iterator[int]:
    """Prefer renewable OAuth; otherwise lend a validated access-only token."""

    with ExitStack() as stack:
        try:
            descriptor = stack.enter_context(_oauth_subscription_credential(
                requested_runtime_s=requested_runtime_s,
                credential_path=credential_path,
                clock=clock,
                run_status=run_status,
                claude_binary=claude_binary,
                sandbox_wrapper=sandbox_wrapper,
            ))
        except ClaudeCredentialError:
            descriptor = token_snapshot()
            if descriptor is None:
                raise
            stack.callback(os.close, descriptor)
        yield descriptor


# Former claude_token.py: names and call signatures are preserved.

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import http.client
import json
import os
import pwd
import re
from pathlib import Path

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_FILE_ENV = DEFAULT_SETTINGS.env_name("CLAUDE_TOKEN_FILE")
TOKEN_PATTERN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{40,512}")


def _validate_remote_token(token: str) -> None:
    """Authenticate inference-only tokens using Claude Code's token-count probe.

    Setup tokens lack the profile scopes required by /api/oauth/validate.
    Pin TLS origin, ignore proxies, refuse redirects, and discard response text.
    Counting a fixed input authenticates without generating a model response.
    """

    connection = http.client.HTTPSConnection("api.anthropic.com", timeout=10)
    valid = False
    try:
        connection.request(
            "POST", "/v1/messages/count_tokens?beta=true",
            body=json.dumps({
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "."}],
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20,token-counting-2024-11-01",
            },
        )
        response = connection.getresponse()
        if 200 <= response.status < 300:
            payload = response.read(16385)
            if len(payload) <= 16384:
                decoded = json.loads(payload)
                if isinstance(decoded, dict):
                    count = decoded.get("input_tokens")
                    valid = type(count) is int and count >= 0
    except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError):
        pass
    finally:
        connection.close()
    if not valid:
        raise ClaudeCredentialUnavailable("Claude long-lived token validation failed")


def _read_host_token_file(path: Path) -> bytes:
    """Walk pinned directories without following aliases or entering a repo."""

    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeClaudeCredential("Claude token file path is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    try:
        for component in (*path.parts[1:-1], None):
            try:
                os.stat(".git", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise UnsafeClaudeCredential("Claude token file must be outside repositories")
            if component is not None:
                child = os.open(component, flags, dir_fd=directory)
                os.close(directory)
                directory = child
        return _read_private_payload(
            Path(path.name), directory_fd=directory, single_link=True
        )
    except FileNotFoundError as exc:
        raise ClaudeCredentialUnavailable("Claude token file is unavailable") from exc
    except OSError:
        raise UnsafeClaudeCredential("Claude token file path is unsafe") from None
    finally:
        os.close(directory)


def _default_token_path() -> Path:
    """Use the OS account state root, independent of desktop HOME/XDG state."""

    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        raise ClaudeCredentialUnavailable("Claude account home is unavailable") from None
    if not home.is_absolute() or home == Path("/"):
        raise UnsafeClaudeCredential("Claude account home is unsafe")
    return get_settings().state_path(home) / "claude-token"


def token_snapshot() -> int | None:
    """Read only host-selected sources; never install a token in durable state."""

    path = os.environ.get(get_settings().env_name("CLAUDE_TOKEN_FILE"))
    source = "token-file"
    if path is None and TOKEN_ENV not in os.environ:
        path = str(_default_token_path())
        source = "token-file(default)"
    if path is not None:
        if not path or not Path(path).is_absolute():
            raise UnsafeClaudeCredential("Claude token file path is invalid")
        try:
            token = _read_host_token_file(Path(path)).decode("ascii").removesuffix("\n")
        except ClaudeCredentialUnavailable as exc:
            if source == "token-file(default)" and isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise
        except UnicodeDecodeError:
            raise UnsafeClaudeCredential("Claude long-lived token is malformed") from None
    else:
        token = os.environ.get(TOKEN_ENV)
        source = "token-env"
    if token is None:
        raise ClaudeCredentialUnavailable("Claude long-lived token is unavailable")
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise UnsafeClaudeCredential("Claude long-lived token is malformed")
    _validate_remote_token(token)
    return _snapshot_descriptor(json.dumps(
        {"claudeCodeOauthToken": token, "source": source}, separators=(",", ":")
    ).encode())


def _snapshot(fd: int) -> dict:

    try:
        decoded = json.loads(os.pread(fd, MAX_CREDENTIAL_BYTES + 1, 0))
    except (OSError, ValueError):
        raise UnsafeClaudeCredential("Claude snapshot is invalid") from None
    if not isinstance(decoded, dict):
        raise UnsafeClaudeCredential("Claude snapshot is invalid")
    return decoded


def snapshot_token(fd: int) -> str | None:

    decoded = _snapshot(fd)
    if "claudeCodeOauthToken" not in decoded:
        return None
    token = decoded["claudeCodeOauthToken"]
    if (not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None
            or decoded.get("source") not in {"token-env", "token-file", "token-file(default)"}):
        raise UnsafeClaudeCredential("Claude token snapshot is invalid")
    return token


def snapshot_source(fd: int) -> str:
    if snapshot_token(fd) is None:
        return "oauth-file"
    return _snapshot(fd)["source"]


def protected_token_available() -> bool:
    """Used only by host readiness; workers never receive this broker marker."""

    try:
        return snapshot_token(int(os.environ[get_settings().env_name("CLAUDE_AUTH_FD")])) is not None
    except (KeyError, ValueError, OSError, ClaudeCredentialError):
        return False
