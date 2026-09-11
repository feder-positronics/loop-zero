"""Cursor CLI adapter using its structured stream-json protocol."""

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .contract import (
    MAX_STRUCTURED_OUTPUT_BYTES,
    ReadinessFailure,
    RuntimeEvent,
    RuntimeHandle,
    RuntimeProgressCallback,
    RuntimeReadiness,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
    SubscriptionEligibility,
    TerminalReason,
)
from .process import (
    ProcessHandle,
    ProcessResult,
    cancel_cli,
    filtered_child_environment,
    run_cli,
)

MAX_RETAINED_EVENTS = 256
MAX_EVENT_LABEL_CHARS = 64
MAX_FINAL_OUTPUT_BYTES = MAX_STRUCTURED_OUTPUT_BYTES
MAX_AUTH_STATUS_BYTES = 256 * 1024
AUTH_STATUS_TIMEOUT_S = 5.0
AUTH_STATUS_COMMAND = ["cursor-agent", "status", "--format", "json"]
SUBSCRIPTION_STATUS_COMMAND = ["cursor-agent", "about", "--format", "json"]
ELIGIBLE_SUBSCRIPTION_TIERS = frozenset(
    {
        "business",
        "enterprise",
        "premium",
        "pro",
        "pro+",
        "standard",
        "team",
        "teams",
        "ultra",
    }
)


class CursorProtocolError(ValueError):
    """Cursor emitted invalid or incomplete stream-json output."""


class CursorParserCause(StrEnum):
    """Closed, content-free reasons why a Cursor event stream was rejected."""

    EMPTY_STREAM = "empty-stream"
    EVENT_AFTER_TERMINAL = "event-after-terminal"
    EVENT_NOT_OBJECT = "event-not-object"
    FIELD_NOT_IDENTIFIER = "field-not-identifier"
    FIELD_NOT_STRING = "field-not-string"
    FIELD_TOO_LONG = "field-too-long"
    INVALID_JSON = "invalid-json"
    REQUIRED_FIELD_MISSING = "required-field-missing"
    TERMINAL_INCOMPLETE = "terminal-incomplete"
    TERMINAL_MISSING = "terminal-missing"
    TERMINAL_OUTPUT_MISSING = "terminal-output-missing"
    TERMINAL_OUTPUT_TOO_LARGE = "terminal-output-too-large"


class CursorParserField(StrEnum):
    """Allow-listed Cursor field names safe to expose as parser context."""

    IS_ERROR = "is_error"
    MODEL = "model"
    REQUEST_ID = "request_id"
    RESULT = "result"
    SESSION_ID = "session_id"
    SUBTYPE = "subtype"
    TYPE = "type"


class CursorStreamError(CursorProtocolError):
    """A stream rejection carrying only an allow-listed parser cause."""

    def __init__(
        self,
        cause: CursorParserCause,
        *,
        field: CursorParserField | None = None,
    ) -> None:
        self.cause = cause
        self.field = field
        super().__init__(self.diagnostic)

    @property
    def diagnostic(self) -> str:
        context = f"; field={self.field.value}" if self.field is not None else ""
        return f"Cursor parser cause: {self.cause.value}{context}"


@dataclass(frozen=True, slots=True)
class CursorAuthStatus:
    """Sanitized browser-login and paid-subscription evidence."""

    logged_in: bool
    auth_method: str
    has_subscription: bool


@dataclass(frozen=True, slots=True)
class ParsedCursorStream:
    """Sanitized Cursor event stream and its required terminal metadata."""

    status: RuntimeStatus
    terminal_reason: TerminalReason
    session_id: str | None
    effective_model: str | None
    request_id: str | None
    events: tuple[RuntimeEvent, ...]
    final_output: str = field(repr=False)


def filtered_cursor_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the native-login environment without API or gateway overrides."""
    return filtered_child_environment(base)


def _auth_payload(stdout: str, command: str) -> dict[str, object]:
    if len(stdout.encode("utf-8")) > MAX_AUTH_STATUS_BYTES:
        raise CursorProtocolError(f"Cursor {command} output is too large")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CursorProtocolError(
            f"Cursor {command} output was not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise CursorProtocolError(f"Cursor {command} output must be an object")
    return payload


def parse_cursor_auth_status(
    status_stdout: str,
    subscription_stdout: str,
) -> CursorAuthStatus:
    """Require browser-login tokens and an allow-listed paid Cursor tier."""
    status = _auth_payload(status_stdout, "status")
    if (
        status.get("isAuthenticated") is not True
        or status.get("hasAccessToken") is not True
        or status.get("hasRefreshToken") is not True
    ):
        raise CursorProtocolError("Cursor status did not prove browser login")

    subscription = _auth_payload(subscription_stdout, "about")
    tier = subscription.get("subscriptionTier")
    if not isinstance(tier, str) or not tier.strip():
        raise CursorProtocolError("Cursor subscription tier was missing")
    if tier.strip().casefold() not in ELIGIBLE_SUBSCRIPTION_TIERS:
        raise CursorProtocolError("Cursor subscription tier was ineligible")
    return CursorAuthStatus(
        logged_in=True,
        auth_method="browser-login",
        has_subscription=True,
    )


def build_cursor_command(
    request: RuntimeRequest,
    *,
    isolated_workspace: Path,
) -> list[str]:
    """Build the current Cursor invocation with structured rather than text output."""
    command = [
        "cursor-agent",
        "-p",
        "--model",
        request.requested_model,
        "--output-format",
        "stream-json",
        "--workspace",
        str(isolated_workspace),
        "--add-dir",
        str(request.cwd),
        "--skip-worktree-setup",
        "--trust",
    ]
    if request.resume_session_id is not None:
        command.extend(("--resume", request.resume_session_id))
    if request.read_only:
        command.extend(("--mode", "ask"))
    else:
        command.append("--force")
    return command


def _optional_string(
    event: dict[str, Any], name: CursorParserField
) -> str | None:
    value = event.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CursorStreamError(CursorParserCause.FIELD_NOT_STRING, field=name)
    if len(value) > 256:
        raise CursorStreamError(CursorParserCause.FIELD_TOO_LONG, field=name)
    return value


def _event_label(
    event: dict[str, Any],
    name: CursorParserField,
    *,
    required: bool = False,
) -> str | None:
    """Accept only bounded identifier-like event metadata safe for diagnostics."""
    value = event.get(name)
    if value is None:
        if required:
            raise CursorStreamError(
                CursorParserCause.REQUIRED_FIELD_MISSING,
                field=name,
            )
        return None
    allowed_punctuation = "._:/-"
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EVENT_LABEL_CHARS
        or not all(
            character.isascii()
            and (character.isalnum() or character in allowed_punctuation)
            for character in value
        )
    ):
        raise CursorStreamError(
            CursorParserCause.FIELD_NOT_IDENTIFIER,
            field=name,
        )
    return value


def _required_final_output(event: dict[str, Any]) -> str:
    value = event.get(CursorParserField.RESULT)
    if not isinstance(value, str):
        raise CursorStreamError(CursorParserCause.TERMINAL_OUTPUT_MISSING)
    if len(value.encode("utf-8")) > MAX_FINAL_OUTPUT_BYTES:
        raise CursorStreamError(CursorParserCause.TERMINAL_OUTPUT_TOO_LARGE)
    return value


def parse_cursor_stream(stream: str) -> ParsedCursorStream:
    """Validate Cursor NDJSON while retaining only safe event metadata.

    Unknown event kinds and fields are deliberately accepted for forward
    compatibility.  Prompt, assistant text, tool arguments/results, paths, and
    raw errors are never copied into the normalized value.
    """
    events: list[RuntimeEvent] = []
    session_id: str | None = None
    effective_model: str | None = None
    request_id: str | None = None
    final_output: str | None = None
    terminal: tuple[RuntimeStatus, TerminalReason] | None = None
    saw_event = False

    for line in stream.split("\n"):
        if not line.strip():
            continue
        if terminal is not None:
            raise CursorStreamError(CursorParserCause.EVENT_AFTER_TERMINAL)
        saw_event = True
        try:
            raw_event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CursorStreamError(CursorParserCause.INVALID_JSON) from exc
        if not isinstance(raw_event, dict):
            raise CursorStreamError(CursorParserCause.EVENT_NOT_OBJECT)
        kind = _event_label(raw_event, CursorParserField.TYPE, required=True)
        assert kind is not None
        subtype_value = _event_label(raw_event, CursorParserField.SUBTYPE)
        event_metadata = RuntimeEvent(kind=kind, subtype=subtype_value)
        if len(events) < MAX_RETAINED_EVENTS:
            events.append(event_metadata)
        elif kind == "result":
            events[-1] = event_metadata

        event_session_id = _optional_string(raw_event, CursorParserField.SESSION_ID)
        if event_session_id is not None:
            session_id = event_session_id
        if kind == "system" and subtype_value == "init":
            model = _optional_string(raw_event, CursorParserField.MODEL)
            if model is not None:
                effective_model = model
        if kind != "result":
            continue
        if terminal is not None:
            raise CursorStreamError(CursorParserCause.EVENT_AFTER_TERMINAL)
        if subtype_value is None or not isinstance(
            raw_event.get(CursorParserField.IS_ERROR), bool
        ):
            raise CursorStreamError(CursorParserCause.TERMINAL_INCOMPLETE)
        request_id = _optional_string(raw_event, CursorParserField.REQUEST_ID)
        final_output = _required_final_output(raw_event)
        if subtype_value == "success" and raw_event["is_error"] is False:
            terminal = (RuntimeStatus.COMPLETED, TerminalReason.COMPLETED)
        else:
            terminal = (RuntimeStatus.FAILED, TerminalReason.PROCESS_EXIT)

    if not saw_event:
        raise CursorStreamError(CursorParserCause.EMPTY_STREAM)
    if terminal is None:
        raise CursorStreamError(CursorParserCause.TERMINAL_MISSING)
    if final_output is None:
        raise CursorStreamError(CursorParserCause.TERMINAL_OUTPUT_MISSING)
    return ParsedCursorStream(
        status=terminal[0],
        terminal_reason=terminal[1],
        session_id=session_id,
        effective_model=effective_model,
        request_id=request_id,
        events=tuple(events),
        final_output=final_output,
    )


class CursorAdapter:
    """Cursor CLI transport with fail-closed structured output parsing."""

    transport = "cursor/cli-stream-json"

    def __init__(
        self,
        *,
        run_cli: Callable[..., ProcessResult] = run_cli,
        run_probe: Callable[..., ProcessResult] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._run_cli = run_cli
        self._run_probe = run_probe or run_cli
        self._which = which
        self._settings = get_settings()

    @using_adapter_settings
    def probe(self, request: RuntimeRequest) -> RuntimeReadiness:
        if request.eligibility is not SubscriptionEligibility.APPROVED:
            return RuntimeReadiness(
                ready=False,
                eligibility=request.eligibility,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="confirm the approved Cursor subscription login path",
            )
        if self._which("cursor-agent") is None:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.EXECUTABLE_MISSING,
                repair="install or restore the approved Cursor CLI",
            )
        outcomes: list[ProcessResult] = []
        try:
            for command in (AUTH_STATUS_COMMAND, SUBSCRIPTION_STATUS_COMMAND):
                outcomes.append(
                    self._run_probe(
                        command,
                        cwd=request.cwd,
                        input_text="",
                        timeout_s=AUTH_STATUS_TIMEOUT_S,
                        env=filtered_cursor_environment(),
                    )
                )
        except OSError:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="run cursor-agent login for the approved subscription",
            )
        if any(
            outcome.timed_out or outcome.output_limited or outcome.returncode != 0
            for outcome in outcomes
        ):
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.UNAVAILABLE,
                failure=ReadinessFailure.AUTHENTICATION,
                repair="run cursor-agent login for the approved subscription",
            )
        try:
            auth = parse_cursor_auth_status(outcomes[0].stdout, outcomes[1].stdout)
        except CursorProtocolError:
            return RuntimeReadiness(
                ready=False,
                eligibility=SubscriptionEligibility.AMBIGUOUS,
                failure=ReadinessFailure.SUBSCRIPTION_UNAVAILABLE,
                repair="confirm Cursor reports browser login and a paid subscription",
            )
        return RuntimeReadiness(
            ready=auth.logged_in and auth.has_subscription,
            eligibility=SubscriptionEligibility.APPROVED,
        )

    @using_adapter_settings
    def cancel(self, handle: RuntimeHandle) -> None:
        if not isinstance(handle, ProcessHandle):
            raise TypeError("Cursor cancellation requires an owned process handle")
        cancel_cli(handle)

    @using_adapter_settings
    def run(
        self,
        request: RuntimeRequest,
        *,
        on_progress: RuntimeProgressCallback | None = None,
    ) -> RuntimeResult:
        del on_progress
        readiness = self.probe(request)
        if not readiness.ready:
            return RuntimeResult(
                vendor=request.vendor,
                transport=self.transport,
                requested_model=request.requested_model,
                status=RuntimeStatus.SUBSCRIPTION_UNAVAILABLE,
                terminal_reason=TerminalReason.SUBSCRIPTION_UNAVAILABLE,
                attempt_id=request.attempt_id,
                eligibility=readiness.eligibility,
                fallback_from=request.fallback_from,
                diagnostics=(readiness.repair or "Cursor CLI unavailable",),
            )
        try:
            workspace_root = get_settings().workspace_root(
                request.tooling_root or request.cwd
            )
            workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            workspace_root = workspace_root.resolve(strict=True)
            with TemporaryDirectory(
                prefix=get_settings().temp_name("cursor-cli"),
                dir=workspace_root,
            ) as directory:
                isolated_workspace = Path(directory)
                outcome = self._run_cli(
                    build_cursor_command(
                        request,
                        isolated_workspace=isolated_workspace,
                    ),
                    cwd=isolated_workspace,
                    input_text=(
                        "The governed worktree is "
                        f"{request.cwd}. Treat that absolute path as the sole "
                        "working directory for this task.\n\n"
                        f"{request.prompt}"
                    ),
                    timeout_s=request.timeout_s,
                    env=filtered_cursor_environment(),
                )
        except OSError:
            return RuntimeResult(
                vendor=request.vendor,
                transport=self.transport,
                requested_model=request.requested_model,
                status=RuntimeStatus.FAILED,
                terminal_reason=TerminalReason.STARTUP_FAILURE,
                attempt_id=request.attempt_id,
                eligibility=request.eligibility,
                fallback_from=request.fallback_from,
                diagnostics=("Cursor CLI startup failed",),
            )
        if outcome.timed_out:
            return self._result(
                request,
                status=RuntimeStatus.TIMED_OUT,
                reason=TerminalReason.TIMEOUT,
                outcome=outcome,
                diagnostics=("Cursor process timed out",),
            )
        if outcome.output_limited:
            return self._result(
                request,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROTOCOL_FAILURE,
                outcome=outcome,
                diagnostics=("Cursor output exceeded the safe limit",),
            )
        try:
            parsed = parse_cursor_stream(outcome.stdout)
        except CursorStreamError as exc:
            reason = (
                TerminalReason.PROCESS_EXIT
                if outcome.returncode != 0 and not outcome.stdout.strip()
                else TerminalReason.MALFORMED_EVENT
            )
            diagnostic = (
                "Cursor process exited without a terminal stream event"
                if reason is TerminalReason.PROCESS_EXIT
                else "cursor emitted malformed stream-json"
            )
            return self._result(
                request,
                status=RuntimeStatus.FAILED,
                reason=reason,
                outcome=outcome,
                diagnostics=(diagnostic, exc.diagnostic),
            )
        if outcome.returncode != 0 and parsed.status is RuntimeStatus.COMPLETED:
            return self._result(
                request,
                status=RuntimeStatus.FAILED,
                reason=TerminalReason.PROCESS_EXIT,
                outcome=outcome,
                session_id=parsed.session_id,
                effective_model=parsed.effective_model,
                request_id=parsed.request_id,
                events=parsed.events,
                diagnostics=("Cursor process exited unsuccessfully",),
            )
        return self._result(
            request,
            status=parsed.status,
            reason=parsed.terminal_reason,
            outcome=outcome,
            session_id=parsed.session_id,
            effective_model=parsed.effective_model,
            request_id=parsed.request_id,
            events=parsed.events,
            final_output=parsed.final_output,
        )

    def _result(
        self,
        request: RuntimeRequest,
        *,
        status: RuntimeStatus,
        reason: TerminalReason,
        outcome: ProcessResult,
        session_id: str | None = None,
        effective_model: str | None = None,
        request_id: str | None = None,
        events: tuple[RuntimeEvent, ...] = (),
        diagnostics: tuple[str, ...] = (),
        final_output: str | None = None,
    ) -> RuntimeResult:
        return RuntimeResult(
            vendor=request.vendor,
            transport=self.transport,
            requested_model=request.requested_model,
            status=status,
            terminal_reason=reason,
            attempt_id=request.attempt_id,
            effective_model=effective_model,
            session_id=session_id,
            request_id=request_id,
            eligibility=request.eligibility,
            fallback_from=request.fallback_from,
            events=events,
            diagnostics=diagnostics,
            returncode=outcome.returncode,
            duration_s=outcome.duration_s,
            final_output=final_output,
        )


# Former cursor_credential.py: names and call signatures are preserved.

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import base64
import fcntl
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_CREDENTIAL_BYTES = 1024 * 1024
REFRESH_SAFETY_MARGIN_S = 5 * 60
CURSOR_AUTH_FD_ENV = DEFAULT_SETTINGS.env_name("CURSOR_AUTH_FD")
CURSOR_AUTH_STATE_ENV = DEFAULT_SETTINGS.env_name("CURSOR_AUTH_STATE")


class CursorCredentialError(RuntimeError):
    """The host broker could not produce a safe Cursor credential snapshot."""


class UnsafeCursorCredential(CursorCredentialError):
    """The Cursor credential violated the safety contract."""


class CursorCredentialUnavailable(CursorCredentialError):
    """The protected Cursor credential is unavailable."""


class CursorCredentialExpired(CursorCredentialError):
    """The Cursor browser credential cannot cover the requested run."""


def _jwt_expiry(token: str, *, name: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        raise UnsafeCursorCredential(f"Cursor credential {name} is invalid")
    try:
        padding = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeCursorCredential(f"Cursor credential {name} is invalid") from exc
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise UnsafeCursorCredential(f"Cursor credential {name} is invalid")
    parsed = int(expiry)
    if parsed != expiry or parsed <= 0:
        raise UnsafeCursorCredential(f"Cursor credential {name} is invalid")
    return parsed


def _read_credential(path: Path) -> tuple[bytes, int]:
    if path.is_symlink():
        raise UnsafeCursorCredential("Cursor credential is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CursorCredentialUnavailable("Cursor credential is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 2
            or metadata.st_size > MAX_CREDENTIAL_BYTES
        ):
            raise UnsafeCursorCredential("Cursor credential is unsafe")
        payload = os.pread(descriptor, metadata.st_size, 0)
        if len(payload) != metadata.st_size:
            raise UnsafeCursorCredential("Cursor credential read was incomplete")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeCursorCredential("Cursor credential JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise UnsafeCursorCredential("Cursor credential JSON is invalid")
    access_token = decoded.get("accessToken")
    refresh_token = decoded.get("refreshToken")
    if not isinstance(access_token, str) or not access_token:
        raise UnsafeCursorCredential("Cursor credential accessToken is invalid")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise UnsafeCursorCredential("Cursor credential refreshToken is invalid")
    snapshot = json.dumps(
        {"accessToken": access_token, "refreshToken": access_token},
        separators=(",", ":"),
    ).encode("utf-8")
    return snapshot, _jwt_expiry(access_token, name="accessToken")


def _snapshot_descriptor(payload: bytes) -> int:
    if hasattr(os, "memfd_create"):
        descriptor = os.memfd_create(
            get_settings().memfd_name("cursor-auth"), os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
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
    writable, raw_path = tempfile.mkstemp(prefix=get_settings().temp_name("cursor-auth"))
    path = Path(raw_path)
    try:
        written = 0
        while written < len(payload):
            written += os.write(writable, payload[written:])
        os.fchmod(writable, 0o400)
        readonly = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    finally:
        os.close(writable)
        path.unlink(missing_ok=True)
    return readonly


@contextmanager
def cursor_subscription_credential(
    *,
    requested_runtime_s: float,
    credential_path: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> Iterator[int]:
    """Lend one sealed browser-auth snapshot if it covers the whole run."""
    if requested_runtime_s <= 0:
        raise ValueError("requested_runtime_s must be positive")
    path = credential_path or Path.home() / ".config" / "cursor" / "auth.json"
    payload, expires_at_s = _read_credential(path)
    horizon_s = clock() + requested_runtime_s + REFRESH_SAFETY_MARGIN_S
    if expires_at_s <= horizon_s:
        raise CursorCredentialExpired(
            "Cursor browser login expires before the requested run can finish"
        )
    snapshot = _snapshot_descriptor(payload)
    try:
        yield snapshot
    finally:
        os.close(snapshot)
