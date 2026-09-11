"""Codex Python SDK/app-server and same-vendor CLI runtime adapter.

The system-Python dispatcher never imports ``openai_codex``.  It starts
``sdk_bridge.py`` with the repository-managed interpreter and consumes the
small, allow-listed JSONL protocol implemented by the bridge.
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
from tempfile import TemporaryDirectory
from typing import Protocol

from .codex_isolation import (
    codex_bootstrap_overrides,
    codex_config_lock_override,
    codex_runtime_overrides,
    exported_codex_config_lock,
)
from .contract import (
    MAX_PROTOCOL_LINE_BYTES,
    MAX_STRUCTURED_OUTPUT_BYTES,
    ReadinessFailure,
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

CODEX_SDK_TRANSPORT = "codex/app-server"
CODEX_AUTH_FD_ENV = DEFAULT_SETTINGS.env_name("CODEX_AUTH_FD")
CODEX_CLI_TRANSPORT = "codex/cli"
CODEX_AUTO_TRANSPORTS = frozenset({"codex", "codex/auto"})
AUTH_STATUS_COMMAND = ["codex", "login", "status"]
AUTH_STATUS_TIMEOUT_S = 5.0
SDK_IMPORT_TIMEOUT_S = 5.0
CONFIG_BOOTSTRAP_TIMEOUT_S = 10.0
MAX_AUTH_STATUS_BYTES = 256 * 1024
MAX_BRIDGE_LINE_BYTES = MAX_PROTOCOL_LINE_BYTES
MAX_RETAINED_EVENTS = 256
MAX_DIAGNOSTICS = 4
MAX_METADATA_BYTES = 256
MAX_FINAL_OUTPUT_BYTES = MAX_STRUCTURED_OUTPUT_BYTES
MAX_USAGE_VALUE = 10**12
FIRST_PARTY_MODEL_PROVIDER = "openai"
DEFAULT_SERVICE_TIER = "default"
OUTER_WORKER_SANDBOX_ENV = DEFAULT_SETTINGS.env_name("OUTER_WORKER_SANDBOX")


def protected_codex_credential_ready(
    payload: bytes, *, now_s: float | None = None
) -> bool:
    """Validate the ChatGPT credential shape the pinned app-server can refresh."""
    del now_s  # Retained for compatibility with older focused callers.
    try:
        decoded = json.loads(payload)
        tokens = decoded["tokens"]
    except (
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
    ):
        return False
    if (
        not isinstance(decoded, dict)
        or decoded.get("auth_mode") != "chatgpt"
        or decoded.get("OPENAI_API_KEY") not in (None, "")
        or not isinstance(tokens, dict)
    ):
        return False
    # Access and identity JWTs may expire between governed launches.  The
    # first-party app-server owns refresh and account-type enforcement, so the
    # host boundary proves a refresh-capable ChatGPT credential rather than
    # rejecting a valid login on cached JWT expiry.
    for token_name in ("access_token", "id_token", "refresh_token"):
        token = tokens.get(token_name)
        if not isinstance(token, str) or not token:
            return False
        try:
            encoded_token = token.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if len(encoded_token) > MAX_AUTH_STATUS_BYTES:
            return False
    return True


class RuntimeProcessRunner(Protocol):
    """The bounded process-runner seam shared by Codex transports."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        input_text: str,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> ProcessResult: ...


# Codex-specific billing/gateway overrides beyond the shared raw-API strip.
CODEX_BLOCKED_ENV_VARS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_ENDPOINT",
        "CODEX_API_KEY",
        "CODEX_API_BASE",
        "CODEX_BASE_URL",
    }
)


class CodexProtocolError(ValueError):
    """Codex emitted malformed or incomplete normalized protocol output."""

    def __init__(self, message: str, *, semantic_event: bool = False) -> None:
        super().__init__(message)
        self.semantic_event = semantic_event


@dataclass(frozen=True, slots=True)
class CodexAuthStatus:
    """The only subscription facts retained from Codex login/account evidence."""

    logged_in: bool
    auth_method: str
    has_subscription: bool


@dataclass(frozen=True, slots=True)
class ParsedCodexStream:
    """Bounded Codex metadata and terminal output."""

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


def filtered_codex_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the approved ChatGPT-login environment without API/gateway paths."""
    environment = filtered_child_environment(base)
    for name in CODEX_BLOCKED_ENV_VARS:
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


def _sdk_readiness_payload(request: RuntimeRequest) -> str:
    """Build the no-turn request for the repository-owned app-server probe."""
    return json.dumps(
        {
            "vendor": "codex-probe",
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


def parse_codex_sdk_readiness(stdout: str) -> ReadinessFailure | None:
    """Validate the small, content-free app-server readiness protocol."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > MAX_BRIDGE_LINE_BYTES:
        raise CodexProtocolError("Codex SDK readiness protocol was invalid")
    try:
        frame = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise CodexProtocolError("Codex SDK readiness protocol was invalid") from exc
    if not isinstance(frame, dict) or frame.get("type") != "readiness":
        raise CodexProtocolError("Codex SDK readiness protocol was invalid")
    if frame == {"type": "readiness", "status": "ready"}:
        return None
    failures = {
        "sdk-version": ReadinessFailure.SDK_VERSION_MISMATCH,
        "bootstrap": ReadinessFailure.CONFIG_BOOTSTRAP,
    }
    failure = frame.get("failure")
    if (
        frame.get("status") != "failed"
        or not isinstance(failure, str)
        or failure not in failures
        or set(frame) != {"type", "status", "failure"}
    ):
        raise CodexProtocolError("Codex SDK readiness protocol was invalid")
    return failures[failure]


def map_codex_effort(effort: str) -> str:
    """Map the shared effort ladder onto Codex ReasoningEffort values."""
    if effort == "max":
        return "xhigh"
    return effort


def build_codex_bootstrap_command(export_dir: Path) -> list[str]:
    """Build a local-only CLI command that exports authoritative defaults."""
    command = ["codex"]
    for override in codex_bootstrap_overrides(export_dir):
        command.extend(("-c", override))
    command.extend(("debug", "prompt-input", "config lock bootstrap"))
    return command


def build_codex_command(
    request: RuntimeRequest,
    *,
    config_lock: Path,
    output_schema_path: Path,
) -> list[str]:
    """Build the non-interactive Codex CLI backup command."""
    outer_sandbox = dict(request.capability_profile.environment).get(
        get_settings().env_name("OUTER_WORKER_SANDBOX")
    )
    sandbox = (
        "danger-full-access"
        if outer_sandbox == "1"
        else ("read-only" if request.read_only else "workspace-write")
    )
    effort = map_codex_effort(request.effort)
    command = [
        "codex",
        "-c",
        codex_config_lock_override(config_lock),
    ]
    for override in codex_runtime_overrides():
        command.extend(("-c", override))
    command.extend(
        [
            "exec",
            "-c",
            f"model={request.requested_model}",
            "-c",
            f"model_reasoning_effort={effort}",
            "-c",
            f"model_provider={FIRST_PARTY_MODEL_PROVIDER}",
            "-c",
            f"service_tier={DEFAULT_SERVICE_TIER}",
            "--disable",
            "fast_mode",
            "--sandbox",
            sandbox,
            "--output-schema",
            str(output_schema_path),
            "--add-dir",
            str(request.cwd),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-",
        ]
    )
    return command


build_codex_cli_command = build_codex_command


def _bounded_string(
    value: object,
    name: str,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise CodexProtocolError(f"Codex field {name!r} is required")
        return None
    if not isinstance(value, str) or not value:
        raise CodexProtocolError(f"Codex field {name!r} must be a string")
    if len(value.encode("utf-8")) > MAX_METADATA_BYTES:
        raise CodexProtocolError(f"Codex field {name!r} is too long")
    return value


def parse_codex_login_status(stdout: str) -> CodexAuthStatus:
    """Validate ChatGPT subscription login without retaining account identity."""
    if len(stdout.encode("utf-8")) > MAX_AUTH_STATUS_BYTES:
        raise CodexProtocolError("Codex login status is too large")
    text = stdout.strip()
    if not text:
        raise CodexProtocolError("Codex login status was empty")
    lowered = text.lower()
    if "api key" in lowered or "apikey" in lowered:
        raise CodexProtocolError("Codex login status used an API key")
    if "bedrock" in lowered:
        raise CodexProtocolError("Codex login status used Amazon Bedrock")
    if "personal access token" in lowered or "access token" in lowered:
        raise CodexProtocolError("Codex login status used a token login")
    if lowered not in {"logged in using chatgpt", "logged in using chatgpt."}:
        raise CodexProtocolError(
            "Codex login status was not ChatGPT subscription login"
        )
    return CodexAuthStatus(
        logged_in=True,
        auth_method="chatgpt",
        has_subscription=True,
    )


def parse_codex_account(payload: Mapping[str, object]) -> CodexAuthStatus:
    """Validate SDK account evidence; reject apiKey and Amazon Bedrock paths."""
    account = payload.get("account")
    if account is None:
        raise CodexProtocolError("Codex account was missing")
    if not isinstance(account, Mapping):
        raise CodexProtocolError("Codex account was invalid")
    account_type = account.get("type")
    if account_type in {"apiKey", "amazonBedrock"}:
        raise CodexProtocolError(
            f"Codex account type {account_type!r} is not subscription-backed"
        )
    if account_type != "chatgpt":
        raise CodexProtocolError("Codex account type was not ChatGPT")
    return CodexAuthStatus(
        logged_in=True,
        auth_method="chatgpt",
        has_subscription=True,
    )


parse_auth_status = parse_codex_login_status


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
        nested = usage.get("total") or usage.get("last")
        if isinstance(nested, dict):
            candidates.append(nested)
    if isinstance(model_usage, dict):
        candidates.append(model_usage)
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
            (
                "cache_read_input_tokens",
                "cache_read_tokens",
                "cacheReadInputTokens",
                "cached_input_tokens",
                "cachedInputTokens",
            ),
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
            (
                "reasoning_tokens",
                "reasoningTokens",
                "reasoning_output_tokens",
                "reasoningOutputTokens",
            ),
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
            raise CodexProtocolError("Codex terminal status was invalid")
        wire_status = "failed" if is_error else "completed"
    if not isinstance(wire_status, str):
        raise CodexProtocolError("Codex terminal status was invalid")
    if wire_status not in {RuntimeStatus.COMPLETED.value, RuntimeStatus.FAILED.value}:
        raise CodexProtocolError("Codex terminal status was unknown")
    status = RuntimeStatus(wire_status)

    wire_reason = frame.get("terminal_reason")
    if wire_reason is None:
        wire_reason = (
            TerminalReason.COMPLETED.value
            if status is RuntimeStatus.COMPLETED
            else TerminalReason.PROCESS_EXIT.value
        )
    if not isinstance(wire_reason, str):
        raise CodexProtocolError("Codex terminal reason was invalid")
    try:
        reason = TerminalReason(wire_reason)
    except ValueError as exc:
        raise CodexProtocolError("Codex terminal reason was unknown") from exc
    if (
        reason
        in {
            TerminalReason.BUDGET_EXHAUSTED,
            TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT,
        }
        or (
            status is RuntimeStatus.COMPLETED and reason is not TerminalReason.COMPLETED
        )
        or (status is RuntimeStatus.FAILED and reason is TerminalReason.COMPLETED)
    ):
        raise CodexProtocolError("Codex terminal status and reason were inconsistent")

    output = frame.get("output", frame.get("result"))
    if output is not None and not isinstance(output, str):
        raise CodexProtocolError("Codex terminal output was invalid")
    if isinstance(output, str):
        try:
            output_size = len(output.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise CodexProtocolError(
                "Codex terminal output was not valid UTF-8"
            ) from exc
        if output_size > MAX_FINAL_OUTPUT_BYTES:
            raise CodexProtocolError("Codex terminal output exceeds the safe limit")
    structured_output = frame.get("structured_output")
    if structured_output is not None and not isinstance(structured_output, dict):
        raise CodexProtocolError("Codex structured output was invalid")
    if isinstance(structured_output, dict):
        try:
            encoded_output = json.dumps(
                structured_output,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CodexProtocolError(
                "Codex structured output was not valid UTF-8"
            ) from exc
        if len(encoded_output) > MAX_FINAL_OUTPUT_BYTES:
            raise CodexProtocolError("Codex structured output exceeds the safe limit")
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


def parse_codex_stream(stream: str) -> ParsedCodexStream:
    """Parse bridge JSONL into bounded Codex metadata.

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
            raise CodexProtocolError(
                "Codex protocol line exceeds the safe limit",
                semantic_event=semantic_seen,
            )
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexProtocolError(
                "Codex emitted invalid JSON",
                semantic_event=semantic_seen,
            ) from exc
        if not isinstance(raw, dict):
            raise CodexProtocolError(
                "Codex protocol frames must be objects",
                semantic_event=semantic_seen,
            )
        if terminal is not None:
            raise CodexProtocolError(
                "Codex emitted a frame after its terminal result",
                semantic_event=True,
            )

        frame_type = raw.get("type")
        if frame_type == "event":
            if raw.get("kind") == "command_completion":
                _retain_command_completion(diagnostics, raw, MAX_DIAGNOSTICS)
                continue
            kind = _bounded_string(raw.get("kind"), "kind", required=True)
            assert kind is not None
            subtype = _bounded_string(raw.get("subtype"), "subtype")
            event_semantic = raw.get(
                "semantic",
                kind in {"assistant", "tool_use", "tool_result", "stream"},
            )
            if not isinstance(event_semantic, bool):
                raise CodexProtocolError(
                    "Codex event semantic marker was invalid",
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
                    status,
                    reason,
                    output,
                    frame_structured_output,
                    frame_usage,
                    frame_cost,
                ) = _terminal_from_frame(raw)
            except CodexProtocolError as exc:
                raise CodexProtocolError(
                    str(exc),
                    semantic_event=True,
                ) from exc
            session_id = (
                _bounded_string(raw.get("session_id"), "session_id") or session_id
            )
            effective_model = _effective_model(raw) or effective_model
            request_id = (
                _bounded_string(raw.get("request_id"), "request_id")
                or _bounded_string(raw.get("turn_id"), "turn_id")
                or request_id
            )
            terminal = (status, reason)
            final_output = output
            structured_output = frame_structured_output
            usage = frame_usage
            cost_usd = frame_cost
            _append_event(
                events,
                RuntimeEvent(
                    kind="result",
                    subtype=_bounded_string(raw.get("subtype"), "subtype"),
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
                raise CodexProtocolError(
                    "Codex error frame was unknown",
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
            _retain_priority_diagnostic(
                diagnostics,
                {
                    "startup": "Codex SDK startup failed",
                    "protocol": "Codex SDK protocol failed",
                    "disconnect": "Codex SDK transport disconnected",
                }[error_reason],
                MAX_DIAGNOSTICS,
            )
            continue

        _retain_priority_diagnostic(
            diagnostics, "Codex emitted an unknown protocol frame", MAX_DIAGNOSTICS
        )

    if terminal is None:
        missing_terminal_diagnostic = (
            "Codex emitted an empty protocol stream"
            if not saw_frame
            else "Codex stream ended without a terminal result"
        )
        raise CodexProtocolError(
            missing_terminal_diagnostic,
            semantic_event=semantic_seen,
        )

    return ParsedCodexStream(
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


def parse_codex_cli_output(stdout: str, *, returncode: int) -> ParsedCodexStream:
    """Normalize ``codex exec`` text output into the shared result shape."""
    if len(stdout.encode("utf-8")) > MAX_FINAL_OUTPUT_BYTES:
        raise CodexProtocolError("Codex CLI output exceeds the safe limit")
    output = stdout.rstrip()
    if not output and returncode == 0:
        raise CodexProtocolError("Codex CLI emitted no terminal result")
    status = RuntimeStatus.COMPLETED if returncode == 0 else RuntimeStatus.FAILED
    reason = (
        TerminalReason.COMPLETED
        if status is RuntimeStatus.COMPLETED
        else TerminalReason.PROCESS_EXIT
    )
    try:
        structured_output = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CodexProtocolError("Codex CLI result was not one JSON object") from exc
    if not isinstance(structured_output, dict):
        raise CodexProtocolError("Codex CLI result must be one JSON object")
    return ParsedCodexStream(
        status=status,
        terminal_reason=reason,
        session_id=None,
        effective_model=None,
        request_id=None,
        # ``codex exec`` labels this value only as "tokens used".  The
        # normalized contract has typed input/output/cache/reasoning fields,
        # so treating that aggregate as one of them would be false precision.
        usage=None,
        cost_usd=None,
        events=(RuntimeEvent(kind="result", subtype=None, semantic=True),),
        semantic_event=True,
        diagnostics=(),
        structured_output=structured_output,
    )


def _command_completion_diagnostic(frame: Mapping[str, object]) -> str | None:
    """One closed, content-free projection shared by normal and timeout paths."""
    if (
        set(frame) != {"type", "kind", "semantic", "status", "exit_code", "output_bytes"}
        or frame.get("type") != "event"
        or frame.get("kind") != "command_completion"
        or frame.get("semantic") is not False
        or not isinstance(frame.get("status"), str)
        or frame["status"] not in {"completed", "failed", "declined"}
    ):
        return None
    for key, minimum in (("exit_code", -MAX_USAGE_VALUE), ("output_bytes", 0)):
        value = frame[key]
        if value is not None and (type(value) is not int or not minimum <= value <= MAX_USAGE_VALUE):
            return None
    return (
        f"Codex command completed: status={frame['status']}; "
        f"exit_code={frame['exit_code']}; observed_output_bytes={frame['output_bytes']}"
    )


def _retain_priority_diagnostic(diagnostics: list[str], diagnostic: str, limit: int) -> None:
    """Fixed runtime warnings displace command outcomes, never other warnings."""
    if len(diagnostics) >= limit:
        for index, existing in enumerate(diagnostics):
            if existing.startswith("Codex command completed:"):
                del diagnostics[index]
                break
    if len(diagnostics) < limit:
        diagnostics.append(diagnostic)


def _retain_command_completion(diagnostics: list[str], frame: Mapping[str, object], limit: int) -> None:
    diagnostic = _command_completion_diagnostic(frame)
    if diagnostic is not None:
        if len(diagnostics) < limit:
            diagnostics.append(diagnostic)
        else:
            # Replace only a prior completion; never evict a bridge error label.
            for index in range(len(diagnostics) - 1, -1, -1):
                if diagnostics[index].startswith("Codex command completed:"):
                    diagnostics[index] = diagnostic
                    break


def _timeout_diagnostics(stream: str) -> tuple[str, ...]:
    """Retain bounded diagnostics only; buffered frames confer no result authority."""
    if len(stream) > MAX_BRIDGE_LINE_BYTES:
        return ()
    try:
        if len(stream.encode("utf-8")) > MAX_BRIDGE_LINE_BYTES:
            return ()
    except UnicodeEncodeError:
        return ()
    diagnostics: list[str] = []
    # An interrupted final line is not a complete bridge frame. CLI output is
    # a result object, not this diagnostic protocol, and is never projected.
    for line in stream.split("\n")[:-1]:
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue
            if raw.get("type") == "event" and raw.get("kind") == "command_completion":
                _retain_command_completion(diagnostics, raw, MAX_DIAGNOSTICS - 1)
                continue
            if raw.get("type") != "error":
                continue
            parsed = parse_codex_stream(line)
        except (CodexProtocolError, ValueError, RecursionError):
            continue
        # Keep scanning the bounded stream: a later bridge error outranks an
        # earlier completion even when the diagnostic buffer is already full.
        for diagnostic in parsed.diagnostics:
            _retain_priority_diagnostic(diagnostics, diagnostic, MAX_DIAGNOSTICS - 1)
    return tuple(diagnostics[: MAX_DIAGNOSTICS - 1])


parse_codex_output = parse_codex_stream


class CodexAdapter:
    """Codex SDK/app-server primary with prelaunch and read-only CLI fallback."""

    transport = CODEX_SDK_TRANSPORT

    def __init__(
        self,
        *,
        run_process: RuntimeProcessRunner = default_run_cli,
        run_cli: RuntimeProcessRunner | None = None,
        run_probe: RuntimeProcessRunner | None = None,
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
            "openai_codex",
            timeout_s=SDK_IMPORT_TIMEOUT_S,
            env=filtered_codex_environment(),
        )

    @staticmethod
    def _runtime_write_root(request: RuntimeRequest) -> Path:
        """Return the one root made writable by the outer worker sandbox."""
        write_roots = request.capability_profile.write_roots
        if not write_roots:
            return request.cwd
        if len(write_roots) != 1:
            raise CodexProtocolError(
                "Codex runtime requires exactly one outer writable root"
            )
        return write_roots[0]

    def _auth_readiness(self, request: RuntimeRequest) -> RuntimeReadiness:
        if request.eligibility is not SubscriptionEligibility.APPROVED:
            return RuntimeReadiness(
                ready=False,
                eligibility=request.eligibility,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="confirm the approved Codex ChatGPT subscription login path",
            )
        if self._which("codex") is None:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.EXECUTABLE_MISSING,
                repair="install or restore the approved Codex CLI",
            )
        raw_auth_fd = os.environ.get(get_settings().env_name("CODEX_AUTH_FD"))
        if raw_auth_fd is not None:
            try:
                fd = int(raw_auth_fd)
                metadata = os.fstat(fd)
                payload = os.pread(fd, min(metadata.st_size, 1024 * 1024 + 1), 0)
            except (OSError, ValueError):
                payload = b""
                metadata = None
            if (
                metadata is None
                or metadata.st_size > 1024 * 1024
                or not protected_codex_credential_ready(payload)
            ):
                return RuntimeReadiness(
                    ready=False,
                    eligibility=SubscriptionEligibility.UNAVAILABLE,
                    failure=ReadinessFailure.AUTHENTICATION,
                    repair=(
                        "replace the missing, malformed, or expired protected "
                        "Codex subscription credential"
                    ),
                    transport=CODEX_SDK_TRANSPORT,
                )
            return RuntimeReadiness(
                ready=True,
                eligibility=SubscriptionEligibility.APPROVED,
                transport=CODEX_SDK_TRANSPORT,
            )
        try:
            outcome = self._run_probe(
                AUTH_STATUS_COMMAND,
                cwd=request.cwd,
                input_text="",
                timeout_s=AUTH_STATUS_TIMEOUT_S,
                env=filtered_codex_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="run codex login for the approved ChatGPT subscription",
            )
        if outcome.timed_out or outcome.returncode != 0:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="run codex login for the approved ChatGPT subscription",
            )
        try:
            # Codex currently writes the successful login-status line to
            # stderr, while older builds and test doubles use stdout.
            auth = parse_codex_login_status(outcome.stdout or outcome.stderr)
        except CodexProtocolError:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.AMBIGUOUS,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="confirm Codex login status reports ChatGPT subscription login",
            )
        if (
            not auth.logged_in
            or auth.auth_method != "chatgpt"
            or not auth.has_subscription
        ):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="run codex login for an eligible ChatGPT subscription",
            )
        return RuntimeReadiness(
            ready=True,
            eligibility=SubscriptionEligibility.APPROVED,
            transport=CODEX_CLI_TRANSPORT,
        )

    def _cli_launch_readiness(self, request: RuntimeRequest) -> RuntimeReadiness:
        """Authorize a CLI launch without repeating its config bootstrap."""
        if os.environ.get(get_settings().env_name("CODEX_AUTH_FD")) is not None:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="protected Codex credentials require the app-server transport",
                transport=CODEX_CLI_TRANSPORT,
            )
        return self._auth_readiness(request)

    @using_adapter_settings
    def probe_cli(self, request: RuntimeRequest) -> RuntimeReadiness:
        auth = self._cli_launch_readiness(request)
        if not auth.ready:
            return auth
        try:
            write_root = self._runtime_write_root(request)
            with TemporaryDirectory(
                prefix=get_settings().temp_name("codex-cli-readiness"), dir=write_root
            ) as directory:
                root = Path(directory)
                codex_home = root / "home"
                export_dir = root / "locks"
                codex_home.mkdir(mode=0o700)
                export_dir.mkdir(mode=0o700)
                environment = filtered_codex_environment()
                environment["CODEX_HOME"] = str(codex_home)
                outcome = self._run_process(
                    build_codex_bootstrap_command(export_dir),
                    cwd=root,
                    input_text="",
                    timeout_s=min(CONFIG_BOOTSTRAP_TIMEOUT_S, request.timeout_s),
                    env=environment,
                )
                if (
                    outcome.timed_out
                    or outcome.output_limited
                    or outcome.returncode != 0
                ):
                    raise RuntimeError("bootstrap")
                exported_codex_config_lock(export_dir)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.CONFIG_BOOTSTRAP,
                repair="repair the isolated Codex CLI configuration bootstrap",
                transport=CODEX_CLI_TRANSPORT,
            )
        return RuntimeReadiness(
            ready=True,
            eligibility=auth.eligibility,
            transport=CODEX_CLI_TRANSPORT,
        )

    @using_adapter_settings
    def probe_sdk(self, request: RuntimeRequest) -> RuntimeReadiness:
        python = repository_python(request.tooling_root or request.cwd)
        if os.name != "posix":
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.UNSUPPORTED_PLATFORM,
                repair="use a supported POSIX runtime environment for Codex SDK work",
                transport=CODEX_SDK_TRANSPORT,
            )
        sdk_ready = self._sdk_available(python, request.cwd)
        if not sdk_ready:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.SDK_UNAVAILABLE,
                repair="restore the repository openai-codex tooling dependency",
                transport=CODEX_SDK_TRANSPORT,
            )
        auth = self._auth_readiness(request)
        if not auth.ready:
            return auth
        try:
            outcome = self._run_probe(
                build_sdk_bridge_command(request.tooling_root or request.cwd),
                cwd=request.cwd,
                input_text=_sdk_readiness_payload(request),
                timeout_s=min(CONFIG_BOOTSTRAP_TIMEOUT_S, request.timeout_s),
                env=filtered_codex_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            outcome = None
        if outcome is None or outcome.timed_out or outcome.output_limited:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.CONFIG_BOOTSTRAP,
                repair="repair the isolated Codex app-server bootstrap",
                transport=CODEX_SDK_TRANSPORT,
            )
        try:
            failure = parse_codex_sdk_readiness(outcome.stdout)
        except CodexProtocolError:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.AMBIGUOUS,
                failure=ReadinessFailure.PROTOCOL_INCOMPATIBLE,
                repair="repair the repository Codex app-server readiness protocol",
                transport=CODEX_SDK_TRANSPORT,
            )
        if failure is not None:
            repair = {
                ReadinessFailure.SDK_VERSION_MISMATCH: (
                    "restore the repository-pinned openai-codex SDK version"
                ),
                ReadinessFailure.CONFIG_BOOTSTRAP: (
                    "repair the isolated Codex app-server bootstrap"
                ),
            }[failure]
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=failure,
                repair=repair,
                transport=CODEX_SDK_TRANSPORT,
            )
        if outcome.returncode != 0:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.AMBIGUOUS,
                failure=ReadinessFailure.PROTOCOL_INCOMPATIBLE,
                repair="repair the repository Codex app-server readiness protocol",
                transport=CODEX_SDK_TRANSPORT,
            )
        return RuntimeReadiness(
            ready=True,
            eligibility=auth.eligibility,
            transport=CODEX_SDK_TRANSPORT,
        )

    def _sdk_launch_readiness(self, request: RuntimeRequest) -> RuntimeReadiness:
        """Keep execution's existing short-lived launch guard separate from doctor.

        The governed dispatcher runs the complete no-turn boundary probe before
        launch.  Repeating it inside every worker launch would create a second
        bootstrap with no additional readiness signal and would widen the
        runtime's failure surface after the selected transport is settled.
        """
        python = repository_python(request.tooling_root or request.cwd)
        if os.name != "posix":
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.UNSUPPORTED_PLATFORM,
                repair="use a supported POSIX runtime environment for Codex SDK work",
                transport=CODEX_SDK_TRANSPORT,
            )
        if not self._sdk_available(python, request.cwd):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.SDK_UNAVAILABLE,
                repair="restore the repository openai-codex tooling dependency",
                transport=CODEX_SDK_TRANSPORT,
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
            return replace(request, transport=CODEX_SDK_TRANSPORT), sdk_readiness
        cli_readiness = self.probe_cli(request)
        if cli_readiness.ready:
            return replace(request, transport=CODEX_CLI_TRANSPORT), cli_readiness
        if os.environ.get(get_settings().env_name("CODEX_AUTH_FD")) is not None:
            # The protected credential is transferred only to the exact SDK
            # bridge command. The CLI denial is therefore policy, not the
            # cause of the failed selection; preserve the SDK diagnosis.
            return None, sdk_readiness
        return None, RuntimeReadiness(
            ready=False,
            eligibility=cli_readiness.eligibility,
            failure=cli_readiness.failure or sdk_readiness.failure,
            repair=cli_readiness.repair or sdk_readiness.repair,
        )

    @using_adapter_settings
    def probe(self, request: RuntimeRequest) -> RuntimeReadiness:
        if request.transport == CODEX_CLI_TRANSPORT:
            return self.probe_cli(request)
        if request.transport in CODEX_AUTO_TRANSPORTS:
            selected, readiness = self.select_transport(request)
            if selected is not None:
                return replace(readiness, transport=selected.transport)
            return readiness
        return self.probe_sdk(request)

    @using_adapter_settings
    def cancel(self, handle: RuntimeHandle | ProcessHandle) -> None:
        if not isinstance(handle, ProcessHandle):
            raise TypeError("Codex cancellation requires an owned process handle")
        cancel_cli(handle)

    @using_adapter_settings
    def run(
        self,
        request: RuntimeRequest,
        *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult:
        preferred_attempt: RuntimeTransportAttempt | None = None
        if request.transport in {*CODEX_AUTO_TRANSPORTS, CODEX_SDK_TRANSPORT}:
            sdk_readiness = self._sdk_launch_readiness(request)
            if sdk_readiness.ready:
                request = replace(request, transport=CODEX_SDK_TRANSPORT)
            else:
                fallback_request = replace(
                    request,
                    transport=CODEX_CLI_TRANSPORT,
                )
                cli_readiness = self._cli_launch_readiness(fallback_request)
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
        if request.transport == CODEX_CLI_TRANSPORT:
            readiness = self._cli_launch_readiness(request)
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
        if request.transport != CODEX_SDK_TRANSPORT:
            return self._unavailable(request, self.probe(request))
        return self._run_sdk(request, on_progress=on_progress)

    def _run_cli(self, request: RuntimeRequest) -> RuntimeResult:
        write_root = self._runtime_write_root(request)
        runtime_environment = merge_runtime_cache_environment(
            filtered_codex_environment(),
            request.capability_profile.environment,
            write_root=write_root,
        )
        try:
            with TemporaryDirectory(
                prefix=get_settings().temp_name("codex-cli"), dir=write_root
            ) as directory:
                root = Path(directory)
                codex_home = root / "home"
                export_dir = root / "locks"
                codex_home.mkdir(mode=0o700)
                export_dir.mkdir(mode=0o700)
                bootstrap_environment = filtered_codex_environment()
                bootstrap_environment["CODEX_HOME"] = str(codex_home)
                bootstrap = self._run_process(
                    build_codex_bootstrap_command(export_dir),
                    cwd=root,
                    input_text="",
                    timeout_s=min(CONFIG_BOOTSTRAP_TIMEOUT_S, request.timeout_s),
                    env=bootstrap_environment,
                )
                if bootstrap.timed_out:
                    return self._result(
                        request,
                        outcome=bootstrap,
                        status=RuntimeStatus.TIMED_OUT,
                        reason=TerminalReason.TIMEOUT,
                        diagnostics=("Codex CLI config isolation timed out",),
                    )
                if bootstrap.output_limited or bootstrap.returncode != 0:
                    return self._result(
                        request,
                        outcome=bootstrap,
                        status=RuntimeStatus.FAILED,
                        reason=TerminalReason.STARTUP_FAILURE,
                        diagnostics=("Codex CLI config isolation failed",),
                    )
                config_lock = exported_codex_config_lock(export_dir)
                if request.output_schema is None:
                    raise CodexProtocolError("Codex runtime requires an output schema")
                output_schema_path = root / "result.schema.json"
                output_schema_path.write_text(
                    json.dumps(request.output_schema, separators=(",", ":")),
                    encoding="utf-8",
                )
                output_schema_path.chmod(0o600)
                remaining_timeout_s = request.timeout_s - bootstrap.duration_s
                if remaining_timeout_s <= 0:
                    return self._result(
                        request,
                        outcome=bootstrap,
                        status=RuntimeStatus.TIMED_OUT,
                        reason=TerminalReason.TIMEOUT,
                        diagnostics=("Codex CLI config isolation timed out",),
                    )
                outcome = self._run_process(
                    build_codex_command(
                        request,
                        config_lock=config_lock,
                        output_schema_path=output_schema_path,
                    ),
                    # A write-capable outer sandbox binds only the process cwd.
                    # Launch writable turns from the governed worktree; keep
                    # read-only fallback turns rooted in the empty bootstrap
                    # directory so their source remains read-only twice over.
                    cwd=request.cwd if not request.read_only else root,
                    input_text=(
                        "The governed worktree is "
                        f"{request.cwd}. Treat that absolute path as the sole "
                        "working directory for this task.\n\n"
                        f"{request.prompt}"
                    ),
                    timeout_s=remaining_timeout_s,
                    env=runtime_environment,
                )
        except (OSError, RuntimeError, subprocess.TimeoutExpired, CodexProtocolError):
            return self._failure(
                request,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.STARTUP_FAILURE,
                diagnostics=("Codex CLI startup failed",),
            )
        if outcome.timed_out:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                diagnostics=("Codex CLI timed out",),
            )
        if outcome.output_limited:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Codex CLI output exceeded the safe limit",),
            )
        try:
            parsed = parse_codex_cli_output(
                outcome.stdout, returncode=outcome.returncode
            )
        except CodexProtocolError:
            reason = (
                TerminalReason.PROTOCOL_FAILURE
                if outcome.returncode == 0
                else TerminalReason.STARTUP_FAILURE
            )
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=reason,
                diagnostics=("Codex CLI emitted malformed output",),
            )
        if parsed.status is None or parsed.terminal_reason is None:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Codex CLI emitted no terminal result",),
            )
        return self._result(
            request,
            outcome=outcome,
            status=parsed.status,
            reason=parsed.terminal_reason,
            session_id=parsed.session_id,
            effective_model=parsed.effective_model,
            request_id=parsed.request_id,
            usage=parsed.usage,
            cost_usd=parsed.cost_usd,
            events=parsed.events,
            diagnostics=parsed.diagnostics,
            final_output=parsed.final_output,
            structured_output=parsed.structured_output,
        )

    def _run_sdk(
        self,
        request: RuntimeRequest,
        *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult:
        payload = json.dumps(
            {
                "vendor": "codex",
                "prompt": request.prompt,
                "cwd": str(request.cwd),
                "requested_model": request.requested_model,
                "effort": request.effort,
                "read_only": request.read_only,
                "budget_usd": request.budget_usd,
                "output_schema": request.output_schema,
                "read_roots": [
                    str(root.resolve())
                    for root in request.capability_profile.read_roots
                ],
            },
            ensure_ascii=False,
        )
        try:
            runtime_environment = merge_runtime_cache_environment(
                filtered_codex_environment(),
                request.capability_profile.environment,
                write_root=self._runtime_write_root(request),
            )
            outcome = self._run_process(
                build_sdk_bridge_command(request.tooling_root or request.cwd),
                cwd=request.cwd,
                input_text=payload,
                timeout_s=request.timeout_s,
                env=runtime_environment,
                on_progress=on_progress,
            )
        except subprocess.TimeoutExpired:
            return self._failure(
                request,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                diagnostics=("Codex SDK bridge timed out",),
            )
        except OSError:
            if request.read_only:
                return self._run_read_only_fallback(
                    request, reason=TerminalReason.STARTUP_FAILURE
                )
            return self._failure(
                request,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.STARTUP_FAILURE,
                diagnostics=("Codex SDK bridge startup failed",),
            )
        if outcome.timed_out:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                diagnostics=("Codex SDK bridge timed out", *_timeout_diagnostics(outcome.stdout)),
            )
        if outcome.output_limited:
            return self._result(
                request,
                outcome=outcome,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                diagnostics=("Codex SDK bridge output exceeded the safe limit",),
            )
        try:
            parsed = parse_codex_stream(outcome.stdout)
        except CodexProtocolError as exc:
            if request.read_only and not exc.semantic_event:
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
                diagnostics=("Codex SDK bridge emitted malformed protocol",),
            )
        if parsed.status is None or parsed.terminal_reason is None:
            if request.read_only and not parsed.semantic_event:
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
                diagnostics=("Codex SDK bridge emitted no terminal result",),
            )
        if (
            request.read_only
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
        if outcome.returncode != 0 and status is RuntimeStatus.COMPLETED:
            status = RuntimeStatus.FAILED
            reason = TerminalReason.PROCESS_EXIT
            diagnostics = ("Codex SDK bridge exited unsuccessfully",)
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
            ):
                return result
            diagnostics = list(result.diagnostics)
            _retain_priority_diagnostic(
                diagnostics, "Native runtime progress was dropped", MAX_DIAGNOSTICS
            )
            return replace(result, diagnostics=tuple(diagnostics))

        fallback_request = replace(
            request,
            transport=CODEX_CLI_TRANSPORT,
        )
        readiness = self._cli_launch_readiness(fallback_request)
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
            if os.environ.get(get_settings().env_name("CODEX_AUTH_FD")) is not None:
                original = self._failure(
                    request,
                    status=RuntimeStatus.FAILED,
                    reason=reason,
                    diagnostics=("Codex SDK transport failed",),
                )
                return retain_progress_diagnostic(
                    replace(
                        original,
                        duration_s=duration_s,
                        transport_attempts=(
                            preferred,
                            *unavailable.transport_attempts,
                        ),
                    )
                )
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
            transport=CODEX_SDK_TRANSPORT,
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
        diagnostic = readiness.repair or "Codex subscription transport unavailable"
        return RuntimeResult(
            vendor="codex",
            transport=request.transport,
            requested_model=request.requested_model,
            status=RuntimeStatus.SUBSCRIPTION_UNAVAILABLE,
            terminal_reason=TerminalReason.SUBSCRIPTION_UNAVAILABLE,
            attempt_id=request.attempt_id,
            eligibility=readiness.eligibility,
            fallback_from=request.fallback_from,
            diagnostics=(diagnostic,),
            transport_attempts=(
                RuntimeTransportAttempt(
                    transport=request.transport,
                    requested_model=request.requested_model,
                    phase="readiness",
                    status=RuntimeStatus.SUBSCRIPTION_UNAVAILABLE.value,
                    terminal_reason=TerminalReason.SUBSCRIPTION_UNAVAILABLE.value,
                    failure_class="eligibility",
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
            vendor="codex",
            transport=request.transport,
            requested_model=request.requested_model,
            status=status,
            terminal_reason=reason,
            attempt_id=request.attempt_id,
            eligibility=request.eligibility,
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
        if outcome is not None and outcome.progress_diagnostic:
            _retain_priority_diagnostic(
                bounded_diagnostics,
                "Native runtime progress was dropped",
                MAX_DIAGNOSTICS,
            )
        return RuntimeResult(
            vendor="codex",
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
            eligibility=request.eligibility,
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
