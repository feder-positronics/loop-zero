"""Repository-venv JSONL bridge for Claude and Codex vendor SDKs.

This module is deliberately a process boundary.  The system-Python parent
passes one settled request on stdin; this bridge imports the selected vendor
SDK, emits only normalized JSONL on stdout, and never performs routing,
fallback, telemetry, or completion decisions.
"""

import asyncio
import json
import math
import os
import re
import shlex
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import TYPE_CHECKING, Callable, Literal, NotRequired, TypedDict, cast


# File-path execution also works with -I and no installed loopzero package.
# Give relative imports a package rooted at this trusted file, without changing
# sys.path or loading siblings by file specs. No consumer directory is searched.
if not __package__:
    package_name = "_loopzero_bridge"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[package_name] = package
    __package__ = package_name

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings

def _load_adjacent_codex_isolation() -> ModuleType:
    """Import the package-owned helper without widening interpreter paths."""
    from . import codex
    return codex


def _load_adjacent_contracts() -> ModuleType:
    """Import shared byte limits through the package."""
    from . import contract
    return contract


_codex_isolation = _load_adjacent_codex_isolation()
from .contract import (
    MAX_PROTOCOL_LINE_BYTES,
    MAX_STRUCTURED_OUTPUT_BYTES,
    RUNTIME_PROGRESS_PROTOCOL_VERSION,
    RuntimePhase,
    RuntimeProgressSignal,
    RuntimeToolLabel,
    is_valid_resume_session_id,
)

PINNED_CODEX_VERSION: str = _codex_isolation.PINNED_CODEX_VERSION


def codex_runtime_overrides() -> tuple[str, ...]:
    return _codex_isolation.codex_runtime_overrides()


if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import (
        HookCallback,
        HookContext,
        HookInput,
        HookJSONOutput,
    )
    from openai_codex import ApprovalMode, Sandbox
    from openai_codex.generated.v2_all import ReasoningEffort
    from openai_codex.models import JsonValue

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 256
MAX_FINAL_OUTPUT_BYTES = MAX_STRUCTURED_OUTPUT_BYTES
MAX_BRIDGE_LINE_BYTES = MAX_PROTOCOL_LINE_BYTES
MAX_USAGE_VALUE = 10**12
MAX_SCOPED_TOOLS = 128
RUNTIME_PROGRESS_FD_ENV_VAR = DEFAULT_SETTINGS.env_name("RUNTIME_PROGRESS_FD")
CODEX_AUTH_FD_ENV = DEFAULT_SETTINGS.env_name("CODEX_AUTH_FD")
RUNTIME_PROGRESS_HEARTBEAT_S = 30.0
RUNTIME_PROGRESS_TRANSITION_INTERVAL_S = 0.25
DEFAULT_READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
PACKAGING_EXPLORATION_DENIAL_REASON = (
    "result packaging has started; finish StructuredOutput using the collected evidence"
)
# Visibility is not authorization: explicit evidence scopes may expose
# Grep/Glob so their denial is observable, while only Read and exact
# sha256sum Bash rules are intentionally authorizable.
SCOPED_VISIBLE_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash"})
SCOPED_ALLOWED_TOOL_PATTERN = re.compile(r"^(Read|Bash)\((.+)\)$")
SCOPED_HASH_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")

RAW_API_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_AWS_API_KEY",
        "ANTHROPIC_AWS_BASE_URL",
        "ANTHROPIC_AWS_WORKSPACE_ID",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_ENDPOINT",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_ENDPOINT",
        "CURSOR_API_KEY",
        "CURSOR_API_URL",
        "CURSOR_BASE_URL",
        "CODEX_API_KEY",
        "CODEX_API_BASE",
        "CODEX_BASE_URL",
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
PYTHON_IMPORT_ENV_VARS = frozenset(
    {
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
    }
)


class BridgeInputError(ValueError):
    """The parent supplied an invalid settled request."""


class _ProgressReporter:
    """Thread-safe, content-free phase transitions and fixed-cadence liveness."""

    def __init__(
        self,
        write_frame: Callable[[dict[str, object]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        close_writer: Callable[[], None] | None = None,
    ) -> None:
        self._write_frame = write_frame
        self._clock = clock
        self._close_writer = close_writer
        self._condition = threading.Condition()
        self._current_phase = RuntimePhase.STARTUP
        self._current_last_tool: RuntimeToolLabel | None = None
        self._last_emitted_state: tuple[RuntimePhase, RuntimeToolLabel | None] | None = (
            None
        )
        self._pending_state: tuple[RuntimePhase, RuntimeToolLabel | None] | None = None
        self._last_activity_at = clock()
        self._last_transition_at = float("-inf")
        self._last_heartbeat_at = self._last_activity_at
        self._sequence = 0
        self._started = False
        self._stopping = False
        self._disabled = False
        self._thread: threading.Thread | None = None

    def start(self, *, start_thread: bool = True) -> None:
        with self._condition:
            if self._started:
                return
            self._started = True
            now = self._clock()
            self._last_activity_at = now
            self._last_heartbeat_at = now
            self._emit_locked(
                phase=RuntimePhase.STARTUP,
                signal=RuntimeProgressSignal.TRANSITION,
                now=now,
            )
            if start_thread:
                self._thread = threading.Thread(
                    target=self._run,
                    name="runtime-progress-reporter",
                    daemon=True,
                )
                self._thread.start()

    def observe(
        self,
        phase: RuntimePhase,
        *,
        last_tool: RuntimeToolLabel | None = None,
    ) -> None:
        """Record vendor activity and request a deduplicated phase transition."""
        if not isinstance(phase, RuntimePhase):
            raise TypeError("runtime progress phase must use the closed vocabulary")
        if last_tool is not None and not isinstance(last_tool, RuntimeToolLabel):
            raise TypeError("runtime progress tool must use the closed vocabulary")
        with self._condition:
            if self._stopping:
                return
            now = self._clock()
            self._last_activity_at = now
            self._current_phase = phase
            if last_tool is not None:
                self._current_last_tool = last_tool
            state = (self._current_phase, self._current_last_tool)
            self._pending_state = (
                state if state != self._last_emitted_state else None
            )
            self._report_due_locked(now)
            self._condition.notify_all()

    def activity(self) -> None:
        """Refresh only the vendor-activity clock without classifying content."""
        with self._condition:
            if self._stopping:
                return
            self._last_activity_at = self._clock()
            self._condition.notify_all()

    def report_due(self) -> None:
        """Evaluate due work; exposed for deterministic contract tests."""
        with self._condition:
            self._report_due_locked(self._clock())

    def _report_due_locked(self, now: float) -> None:
        if (
            self._pending_state is not None
            and now - self._last_transition_at >= RUNTIME_PROGRESS_TRANSITION_INTERVAL_S
        ):
            self._emit_locked(
                phase=self._pending_state[0],
                last_tool=self._pending_state[1],
                signal=RuntimeProgressSignal.TRANSITION,
                now=now,
            )
            self._pending_state = None
        if now - self._last_heartbeat_at >= RUNTIME_PROGRESS_HEARTBEAT_S:
            self._emit_locked(
                phase=self._current_phase,
                last_tool=self._current_last_tool,
                signal=RuntimeProgressSignal.HEARTBEAT,
                now=now,
            )
            self._last_heartbeat_at = now

    def _emit_locked(
        self,
        *,
        phase: RuntimePhase,
        last_tool: RuntimeToolLabel | None = None,
        signal: RuntimeProgressSignal,
        now: float,
    ) -> None:
        if self._disabled:
            return
        self._sequence += 1
        frame: dict[str, object] = {
            "version": RUNTIME_PROGRESS_PROTOCOL_VERSION,
            "phase": phase.value,
            "signal": signal.value,
            "sequence": self._sequence,
            "activity_age_s": round(max(0.0, now - self._last_activity_at), 3),
            "last_tool": last_tool.value if last_tool is not None else None,
        }
        try:
            self._write_frame(frame)
        except (BlockingIOError, InterruptedError):
            # Drop this observation without blocking the SDK. A later transition
            # or heartbeat will carry the latest state once the pipe is writable.
            return
        except Exception:
            self._disabled = True
            return
        if signal is RuntimeProgressSignal.TRANSITION:
            self._last_emitted_state = (phase, last_tool)
            self._last_transition_at = now

    def _run(self) -> None:
        with self._condition:
            while True:
                now = self._clock()
                self._report_due_locked(now)
                if self._stopping and self._pending_state is None:
                    return
                deadlines = [self._last_heartbeat_at + RUNTIME_PROGRESS_HEARTBEAT_S]
                if self._pending_state is not None:
                    deadlines.append(
                        self._last_transition_at
                        + RUNTIME_PROGRESS_TRANSITION_INTERVAL_S
                    )
                self._condition.wait(timeout=max(0.0, min(deadlines) - now))

    def close(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join()
        if self._close_writer is not None:
            try:
                self._close_writer()
            except OSError:
                pass


def _open_progress_reporter_from_env() -> _ProgressReporter | None:
    """Claim the bridge-only write FD before input or vendor SDK imports."""
    marker = os.environ.pop(get_settings().env_name("RUNTIME_PROGRESS_FD"), None)
    if marker is None or not marker.isascii() or not marker.isdecimal():
        return None
    try:
        fd = int(marker)
    except ValueError:
        return None
    if fd <= 2:
        return None
    try:
        os.fstat(fd)
        os.set_inheritable(fd, False)
        os.set_blocking(fd, False)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None

    def write_frame(frame: dict[str, object]) -> None:
        encoded = (
            json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        os.write(fd, encoded)

    reporter = _ProgressReporter(
        write_frame,
        close_writer=lambda: os.close(fd),
    )
    reporter.start()
    return reporter


def _observe_progress(
    reporter: _ProgressReporter | None,
    phase: RuntimePhase,
    *,
    last_tool: RuntimeToolLabel | None = None,
) -> None:
    if reporter is not None:
        reporter.observe(phase, last_tool=last_tool)


def _record_progress_activity(reporter: _ProgressReporter | None) -> None:
    if reporter is not None:
        reporter.activity()


def _claude_tool_label(name: object) -> RuntimeToolLabel:
    """Map known Claude envelopes to a closed label; unknown and MCP stay generic."""
    if name == "Bash":
        return RuntimeToolLabel.COMMAND
    if name in {"Read", "Write", "Edit", "MultiEdit"}:
        return RuntimeToolLabel.FILE
    if name in {"Grep", "Glob"}:
        return RuntimeToolLabel.SEARCH
    if name in {"WebFetch", "WebSearch"}:
        return RuntimeToolLabel.WEB
    return RuntimeToolLabel.TOOL


def _claude_stream_progress(
    event: object,
) -> tuple[RuntimePhase, RuntimeToolLabel | None]:
    """Classify only the SDK block envelope; never retain tool input or names."""
    if not isinstance(event, dict) or event.get("type") != "content_block_start":
        return RuntimePhase.MODEL_ACTIVE, None
    block = event.get("content_block")
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return RuntimePhase.MODEL_ACTIVE, None
    if block.get("name") == "StructuredOutput":
        return RuntimePhase.RESULT_PACKAGING, None
    return RuntimePhase.TOOL_EXECUTION, _claude_tool_label(block.get("name"))


def _claude_stream_progress_phase(event: object) -> RuntimePhase:
    """Compatibility helper for phase-only callers and focused contract tests."""
    return _claude_stream_progress(event)[0]


def _codex_shadow_read(item: object) -> tuple[str, str] | None:
    """Recognize only a simple cat read, never best-effort parsed shell intent."""
    item_id = getattr(item, "id", None)
    if not isinstance(item_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item_id) is None:
        return None
    source = getattr(item, "source", None)
    if getattr(source, "value", source) not in {"agent", "unifiedExecStartup"}:
        return None
    command = getattr(item, "command", None)
    if not isinstance(command, str) or len(command) > 512:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) == 3 and argv[0] in {"/bin/bash", "/usr/bin/bash", "/bin/sh"} and argv[1] in {"-c", "-lc"}:
        command = argv[2]
    # Exact syntax excludes options, redirects, substitutions, shell functions,
    # and compound commands that could fail before opening the target file.
    if command not in {
        f"{cat} {operand}"
        for cat in ("cat", "/bin/cat", "/usr/bin/cat")
        for operand in ("/etc/shadow", "-- /etc/shadow")
    }:
        return None
    actions = getattr(item, "command_actions", ())
    if len(actions) != 1:
        return None
    action = getattr(actions[0], "root", actions[0])
    path = getattr(action, "path", None)
    if (
        getattr(action, "type", None) != "read"
        or getattr(path, "root", path) != "/etc/shadow"
        or getattr(action, "command", None) != command
    ):
        return None
    return item_id, command


def _codex_command_completion(
    item: object, *, started_read: tuple[str, str] | None = None
) -> None:
    """Emit outcome metadata only; a completed command is not a model result."""
    status = getattr(item, "status", None)
    status = getattr(status, "value", status)
    if status not in {"completed", "failed", "declined"}:
        return
    exit_code = getattr(item, "exit_code", None)
    if type(exit_code) is not int or not -MAX_USAGE_VALUE <= exit_code <= MAX_USAGE_VALUE:
        exit_code = None
    output = getattr(item, "aggregated_output", None)
    output_bytes = None
    # Bound counting work; missing/oversized/unencodable output remains unknown.
    # This measures the observed combined string, never total stdout/stderr.
    if isinstance(output, str) and len(output) <= MAX_FINAL_OUTPUT_BYTES:
        try:
            output_bytes = len(output.encode("utf-8"))
        except UnicodeEncodeError:
            pass
    _write_frame({
        "type": "event", "kind": "command_completion", "semantic": False,
        "status": status, "exit_code": exit_code, "output_bytes": output_bytes,
    })
    # Bind started action, completion and aggregated tool output to one SDK
    # item. The commandActions field alone is only best-effort shell parsing.
    completed_read = _codex_shadow_read(item)
    if (
        completed_read is not None
        and started_read == completed_read
        and status in {"completed", "failed"}
        and exit_code is not None
        and exit_code != 0
        and output_bytes is not None
        and any(
            re.fullmatch(
                r"(?:/usr/bin/|/bin/)?cat: (?:/etc/shadow|'/etc/shadow'): "
                r"(?:Permission denied|Operation not permitted)", line
            ) is not None
            for line in output.splitlines()
        )
    ):
        _event_frame(
            kind="tool", subtype="denied:/etc/shadow", semantic=True,
            item_id=completed_read[0],
        )


def _codex_tool_label(item_root: object) -> RuntimeToolLabel:
    """Classify fixed Codex SDK item classes without reading their payloads."""
    type_name = type(item_root).__name__
    if type_name == "CommandExecutionThreadItem":
        return RuntimeToolLabel.COMMAND
    if type_name == "FileChangeThreadItem":
        return RuntimeToolLabel.FILE
    if type_name == "WebSearchThreadItem":
        return RuntimeToolLabel.WEB
    return RuntimeToolLabel.TOOL


class BridgeRequest(TypedDict):
    """Validated input accepted from the system-Python dispatcher."""

    vendor: Literal["claude", "claude-probe", "codex", "codex-probe"]
    prompt: str
    cwd: str
    requested_model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    read_only: bool
    budget_usd: float | None
    output_schema: dict[str, object]
    read_roots: list[str]
    evidence_read_roots: list[str]
    commercial_mode: Literal["subscription-only", "promotional-credit", "owner-paid"]
    visible_tools: NotRequired[list[str]]
    allowed_tools: NotRequired[list[str]]
    resume_session_id: NotRequired[str]


class CodexThreadKwargs(TypedDict):
    """Narrow, typed thread options shared by bootstrap and execution."""

    ephemeral: bool
    model: str
    model_provider: str
    service_tier: str
    sandbox: "Sandbox"
    approval_mode: "ApprovalMode"
    cwd: str


class CodexTurnKwargs(TypedDict):
    """Narrow, typed turn options for the settled runtime request."""

    effort: "ReasoningEffort"
    model: str
    service_tier: str
    sandbox: "Sandbox"
    approval_mode: "ApprovalMode"
    cwd: str
    output_schema: dict[str, "JsonValue"]


CODEX_MODEL_PROVIDER = "openai"
CODEX_SERVICE_TIER = "default"
CLAUDE_MODEL_MINIMUM_CLI_VERSION = {"claude-fable-5-1": (2, 1, 258)}
OUTER_WORKER_SANDBOX_ENV = DEFAULT_SETTINGS.env_name("OUTER_WORKER_SANDBOX")


def _bounded_string(value: object, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise BridgeInputError(f"bridge request field {name!r} is required")
        return None
    if not isinstance(value, str) or not value:
        raise BridgeInputError(f"bridge request field {name!r} is invalid")
    if len(value.encode("utf-8")) > MAX_METADATA_BYTES:
        raise BridgeInputError(f"bridge request field {name!r} is too long")
    return value


def _filtered_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in RAW_API_ENV_VARS | PYTHON_IMPORT_ENV_VARS:
        environment.pop(name, None)
    return environment


def _claude_subscription_environment() -> dict[str, str]:
    """Consume a brokered access-only snapshot without exposing its descriptor."""
    environment = _filtered_environment()
    descriptor_name = get_settings().env_name("CLAUDE_AUTH_FD")
    raw_fd = os.environ.pop(descriptor_name, None)
    environment.pop(descriptor_name, None)
    session_home = get_settings().session_home
    if session_home is not None:
        environment["HOME"] = str(session_home)
    if raw_fd is None:
        return environment
    try:
        fd = int(raw_fd)
        metadata = os.fstat(fd)
        if metadata.st_size <= 2 or metadata.st_size > 1024 * 1024:
            raise BridgeInputError("provider credential descriptor is invalid")
        payload = json.loads(os.pread(fd, metadata.st_size, 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise BridgeInputError("provider credential descriptor is invalid") from exc
    finally:
        try:
            os.close(int(raw_fd))
        except (OSError, ValueError):
            pass
    from .claude import (
        ELIGIBLE_SUBSCRIPTION_TYPES,
        REQUIRED_SCOPES,
        TOKEN_SNAPSHOT_SOURCES,
        is_valid_setup_token,
    )

    token = payload.get("claudeCodeOauthToken") if isinstance(payload, dict) else None
    if token is not None and (
        not is_valid_setup_token(token)
        or payload.get("source") not in TOKEN_SNAPSHOT_SOURCES
    ):
        raise BridgeInputError("provider credential descriptor is invalid")
    if token is None and isinstance(payload, dict):
        oauth = payload.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            raise BridgeInputError("provider credential descriptor is invalid")
        token = oauth.get("accessToken")
        expiry = oauth.get("expiresAt")
        scopes = oauth.get("scopes")
        if (
            not isinstance(token, str)
            or not token
            or oauth.get("refreshToken") != token
            or not isinstance(expiry, int)
            or isinstance(expiry, bool)
            or oauth.get("refreshTokenExpiresAt") != expiry
            or not isinstance(scopes, list)
            or not REQUIRED_SCOPES.issubset(scopes)
            or oauth.get("subscriptionType") not in ELIGIBLE_SUBSCRIPTION_TYPES
        ):
            raise BridgeInputError("provider credential descriptor is invalid")
    if not isinstance(token, str) or not token:
        raise BridgeInputError("provider credential descriptor is invalid")
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return environment


def _materialize_codex_subscription_auth() -> (
    tuple[TemporaryDirectory[str] | None, dict[str, str], Path | None]
):
    """Consume a one-run credential descriptor into a private SDK-only home."""
    raw_fd = os.environ.pop(get_settings().env_name("CODEX_AUTH_FD"), None)
    if raw_fd is None:
        return None, {}, None
    try:
        fd = int(raw_fd)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 1024 * 1024:
                raise BridgeInputError("provider credential is oversized")
            chunks.append(chunk)
    except (OSError, ValueError) as exc:
        raise BridgeInputError("provider credential descriptor is invalid") from exc
    finally:
        try:
            os.close(int(raw_fd))
        except (OSError, ValueError):
            pass
    payload = b"".join(chunks)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeInputError("provider credential is malformed") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise BridgeInputError("provider credential is malformed")
    configured_home = get_settings().session_home
    directory = (
        None
        if configured_home is not None
        else TemporaryDirectory(prefix=get_settings().temp_name("codex-auth"))
    )
    home = configured_home if configured_home is not None else Path(directory.name)
    assert home is not None
    auth_path = home / "auth.json"
    auth_path.write_bytes(payload)
    auth_path.chmod(0o600)
    return directory, {"CODEX_HOME": str(home)}, auth_path


@contextmanager
def _codex_subscription_environment() -> Iterator[tuple[dict[str, str], Path | None]]:
    """Materialize Codex auth and scrub it on every exit path."""
    directory: TemporaryDirectory[str] | None = None
    auth_path: Path | None = None
    try:
        directory, environment, auth_path = _materialize_codex_subscription_auth()
        yield environment, auth_path
    finally:
        if auth_path is not None:
            auth_path.unlink(missing_ok=True)
        if directory is not None:
            directory.cleanup()


def _codex_sdk_environment(
    auth_environment: Mapping[str, str], auth_path: Path | None
) -> dict[str, str]:
    """Build an SDK child environment rooted only in the brokered home."""
    if auth_path is None:
        raise RuntimeError("startup")
    private_home = str(auth_path.parent)
    environment = _filtered_environment()
    environment.pop("CODEX_HOME", None)
    environment.pop("HOME", None)
    environment.update(auth_environment)
    # Pin both lookup roots even if a malformed test seam supplied conflicting
    # values. Codex uses CODEX_HOME directly and other child tooling may use
    # HOME to derive configuration paths.
    environment["CODEX_HOME"] = private_home
    environment["HOME"] = private_home
    return environment


def _bounded_string_list(
    value: object,
    name: str,
    *,
    optional: bool = False,
) -> list[str] | None:
    if value is None and optional:
        return None
    if not isinstance(value, list) or len(value) > MAX_SCOPED_TOOLS:
        raise BridgeInputError(f"bridge request field {name!r} is invalid")
    normalized: list[str] = []
    for item in value:
        parsed = _bounded_string(item, name, required=True)
        assert parsed is not None
        if parsed in normalized:
            raise BridgeInputError(f"bridge request field {name!r} has duplicates")
        normalized.append(parsed)
    return normalized


def _inside_worktree(path: Path, worktree: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(worktree)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _resolve_scoped_path(
    raw_path: str,
    worktree: Path,
    *,
    strict: bool = False,
) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    try:
        return candidate.resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise BridgeInputError("scoped tool path cannot be resolved safely") from exc


def _read_rule_root(specifier: str, worktree: Path) -> tuple[Path, bool]:
    if (
        not specifier.startswith("/")
        or specifier.startswith("//")
        or "\\" in specifier
        or "\x00" in specifier
    ):
        raise BridgeInputError("scoped Read rules must be project-root anchored")
    recursive = specifier.endswith("/**")
    relative = specifier[1:-3] if recursive else specifier[1:]
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise BridgeInputError("scoped Read rule path is invalid")
    if any(character in relative for character in "*?["):
        raise BridgeInputError("scoped Read rules only support a final /**")
    root = (worktree / relative).resolve(strict=False)
    if not _inside_worktree(root, worktree):
        raise BridgeInputError("scoped Read rule escapes the worktree")
    return root, recursive


def _hash_command_paths(command: str, worktree: Path) -> tuple[Path, ...]:
    if any(character in command for character in "\r\n\x00$`;&|<>()\\"):
        raise BridgeInputError("scoped Bash rule contains unsafe shell syntax")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise BridgeInputError("scoped Bash rule is not valid shell syntax") from exc
    if len(tokens) < 3 or tokens[:2] != ["sha256sum", "--"]:
        raise BridgeInputError("scoped Bash rules only allow exact sha256sum commands")
    if not all(SCOPED_HASH_PATH_PATTERN.fullmatch(token) for token in tokens[2:]):
        raise BridgeInputError("scoped Bash rule contains an unsafe path")
    paths = tuple(
        _resolve_scoped_path(token, worktree, strict=True) for token in tokens[2:]
    )
    if not paths or not all(_inside_worktree(path, worktree) for path in paths):
        raise BridgeInputError("scoped Bash rule escapes the worktree")
    return paths


def validate_scoped_tool_configuration(
    *,
    cwd: str | Path,
    read_only: bool,
    visible_tools: object = None,
    allowed_tools: object = None,
) -> tuple[list[str] | None, list[str]]:
    """Validate the narrow Claude SDK authorization boundary before launch."""
    visible = _bounded_string_list(visible_tools, "visible_tools", optional=True)
    allowed = _bounded_string_list(
        [] if allowed_tools is None else allowed_tools,
        "allowed_tools",
    )
    assert allowed is not None
    if visible is None and not allowed:
        return None, []
    if not read_only:
        raise BridgeInputError("scoped Claude tools require read_only mode")
    if visible is None:
        raise BridgeInputError("allowed_tools requires explicit visible_tools")
    unsupported = sorted(set(visible) - SCOPED_VISIBLE_TOOLS)
    if unsupported:
        raise BridgeInputError("visible_tools contains an unsupported tool")
    worktree = Path(cwd).resolve(strict=True)
    for rule in allowed:
        match = SCOPED_ALLOWED_TOOL_PATTERN.fullmatch(rule)
        if match is None:
            raise BridgeInputError("allowed_tools contains an unsupported rule")
        tool_name, specifier = match.groups()
        if tool_name not in visible:
            raise BridgeInputError("allowed tool is not present in visible_tools")
        if tool_name == "Read":
            _read_rule_root(specifier, worktree)
        else:
            _hash_command_paths(specifier, worktree)
    return visible, allowed


def _validate_request_payload(payload: object) -> BridgeRequest:
    """Validate one already-decoded bridge request without retaining secrets."""
    if not isinstance(payload, dict):
        raise BridgeInputError("bridge request must be an object")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or (
        not prompt and payload.get("vendor") not in {"claude-probe", "codex-probe"}
    ):
        raise BridgeInputError("bridge request prompt is invalid")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise BridgeInputError("bridge request prompt exceeds the safe limit")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_dir():
        raise BridgeInputError("bridge request working directory is invalid")
    model = _bounded_string(
        payload.get("requested_model"), "requested_model", required=True
    )
    effort_value = _bounded_string(payload.get("effort"), "effort", required=True)
    if effort_value not in {"low", "medium", "high", "xhigh", "max"}:
        raise BridgeInputError("bridge request effort is invalid")
    read_only = payload.get("read_only")
    if not isinstance(read_only, bool):
        raise BridgeInputError("bridge request read_only is invalid")
    raw_budget = payload.get("budget_usd")
    if raw_budget is None:
        budget_usd = None
    elif (
        isinstance(raw_budget, bool)
        or not isinstance(raw_budget, (int, float))
        or not math.isfinite(raw_budget)
        or raw_budget <= 0
    ):
        raise BridgeInputError("bridge request budget_usd is invalid")
    else:
        budget_usd = float(raw_budget)
    vendor_value = payload.get("vendor", "claude")
    if vendor_value not in {
        "claude",
        "claude-probe",
        "codex",
        "codex-probe",
    }:
        raise BridgeInputError("bridge request vendor is invalid")
    resume_session_id = _bounded_string(
        payload.get("resume_session_id"), "resume_session_id"
    )
    if resume_session_id is not None and not is_valid_resume_session_id(
        resume_session_id
    ):
        raise BridgeInputError("bridge request resume_session_id is invalid")
    if resume_session_id is not None and vendor_value not in {"claude", "codex"}:
        raise BridgeInputError("bridge request resume_session_id is unexpected")
    commercial_mode = payload.get("commercial_mode", "subscription-only")
    if commercial_mode not in {
        "subscription-only",
        "promotional-credit",
        "owner-paid",
    }:
        raise BridgeInputError("bridge request commercial_mode is invalid")
    if vendor_value != "claude" and commercial_mode != "subscription-only":
        raise BridgeInputError("bridge request commercial_mode is unexpected")
    if payload.get("config_lock_target") is not None:
        raise BridgeInputError("bridge config lock target is unexpected")
    output_schema = payload.get(
        "output_schema",
        ({} if vendor_value in {"claude-probe", "codex-probe"} else None),
    )
    if not isinstance(output_schema, dict) or (
        vendor_value not in {"claude-probe", "codex-probe"}
        and not output_schema
    ):
        raise BridgeInputError("bridge request output_schema is invalid")
    encoded_schema = json.dumps(
        output_schema, ensure_ascii=False, separators=(",", ":")
    )
    if len(encoded_schema.encode("utf-8")) > MAX_METADATA_BYTES * 1024:
        raise BridgeInputError("bridge request output_schema exceeds the safe limit")
    raw_read_roots = payload.get(
        "read_roots",
        (
            [cwd]
            if vendor_value in {"claude-probe", "codex-probe"}
            else None
        ),
    )
    if (
        not isinstance(raw_read_roots, list)
        or not raw_read_roots
        or not all(
            isinstance(root, str) and Path(root).is_absolute()
            for root in raw_read_roots
        )
    ):
        raise BridgeInputError("bridge request read_roots are invalid")
    resolved_cwd = Path(cwd).resolve()
    read_roots = [str(Path(root).resolve()) for root in raw_read_roots]
    if read_roots != [str(resolved_cwd)]:
        raise BridgeInputError("bridge request read_roots exceed the governed root")
    raw_evidence_roots = payload.get("evidence_read_roots", [])
    if not isinstance(raw_evidence_roots, list) or len(raw_evidence_roots) > 1:
        raise BridgeInputError("bridge request evidence_read_roots are invalid")
    evidence_read_roots: list[str] = []
    if raw_evidence_roots:
        if vendor_value != "claude" or not read_only:
            raise BridgeInputError(
                "evidence_read_roots require read-only Claude execution"
            )
        if "visible_tools" in payload or "allowed_tools" in payload:
            raise BridgeInputError(
                "evidence_read_roots cannot extend a scoped Claude profile"
            )
        raw_evidence_root = raw_evidence_roots[0]
        if not isinstance(raw_evidence_root, str):
            raise BridgeInputError("bridge request evidence_read_roots are invalid")
        supplied = Path(raw_evidence_root)
        installation_root = (
            get_settings().tooling_root
            or Path(__file__).resolve(strict=True).parents[3]
        )
        expected = installation_root / ".audit" / "dispatch" / "results"
        if (
            not supplied.is_absolute()
            or ".." in supplied.parts
            or raw_evidence_root != str(expected)
            or any(
                candidate.is_symlink()
                for candidate in (
                    installation_root / ".audit",
                    installation_root / ".audit" / "dispatch",
                    expected,
                )
            )
        ):
            raise BridgeInputError("bridge request evidence_read_roots are invalid")
        try:
            resolved_evidence = expected.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BridgeInputError(
                "bridge request evidence_read_roots are unsafe"
            ) from exc
        if resolved_evidence != expected or not resolved_evidence.is_dir():
            raise BridgeInputError("bridge request evidence_read_roots are unsafe")
        evidence_read_roots = [str(resolved_evidence)]
    assert model is not None
    request: BridgeRequest = {
        "vendor": cast(
            Literal["claude", "claude-probe", "codex", "codex-probe"],
            vendor_value,
        ),
        "prompt": prompt,
        "cwd": cwd,
        "requested_model": model,
        "effort": cast(Literal["low", "medium", "high", "xhigh", "max"], effort_value),
        "read_only": read_only,
        "budget_usd": budget_usd,
        "output_schema": cast(dict[str, object], output_schema),
        "read_roots": read_roots,
        "evidence_read_roots": evidence_read_roots,
        "commercial_mode": cast(
            Literal["subscription-only", "promotional-credit", "owner-paid"],
            commercial_mode,
        ),
    }
    if resume_session_id is not None:
        request["resume_session_id"] = resume_session_id
    if vendor_value != "claude" and (
        "visible_tools" in payload or "allowed_tools" in payload
    ):
        raise BridgeInputError("scoped tools are only supported by Claude")
    visible_tools, allowed_tools = validate_scoped_tool_configuration(
        cwd=cwd,
        read_only=read_only,
        visible_tools=payload.get("visible_tools"),
        allowed_tools=payload.get("allowed_tools"),
    )
    if visible_tools is not None:
        request["visible_tools"] = visible_tools
    if allowed_tools:
        request["allowed_tools"] = allowed_tools
    return request


def _read_request() -> BridgeRequest:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise BridgeInputError("bridge request exceeds the safe limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeInputError("bridge request was not valid JSON") from exc
    return _validate_request_payload(payload)


def _write_frame(frame: Mapping[str, object]) -> None:
    encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_BRIDGE_LINE_BYTES:
        raise BridgeInputError("bridge output exceeds the safe limit")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _metadata(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value.encode("utf-8")) > MAX_METADATA_BYTES:
        return None
    return value


def _bounded_structured_output(
    value: dict[str, object],
    *,
    vendor: str,
) -> dict[str, object]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BridgeInputError(
            f"{vendor} structured output was not valid JSON UTF-8"
        ) from exc
    if len(encoded) > MAX_FINAL_OUTPUT_BYTES:
        raise BridgeInputError(f"{vendor} structured output exceeds the safe limit")
    return value


def _usage_payload(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "outputTokens", "completion_tokens"),
        "cache_read_tokens": (
            "cache_read_input_tokens",
            "cache_read_tokens",
            "cacheReadInputTokens",
            "cached_input_tokens",
            "cachedInputTokens",
        ),
        "cache_write_tokens": (
            "cache_creation_input_tokens",
            "cache_write_tokens",
            "cacheCreationInputTokens",
        ),
        "reasoning_tokens": (
            "reasoning_tokens",
            "reasoningTokens",
            "reasoning_output_tokens",
            "reasoningOutputTokens",
        ),
    }
    normalized: dict[str, int] = {}
    for output_name, names in aliases.items():
        for name in names:
            candidate = value.get(name)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and 0 <= candidate <= MAX_USAGE_VALUE
            ):
                normalized[output_name] = candidate
                break
    return normalized or None


def _model_from_usage(value: object) -> str | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    return _metadata(next(iter(value)))


def _claude_api_retry_detail(data: Mapping[str, object]) -> str | None:
    """Summarize a CLI api_retry system event without response bodies."""
    parts: list[str] = []
    attempt = data.get("attempt")
    max_attempts = data.get("max_attempts")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        if isinstance(max_attempts, int) and not isinstance(max_attempts, bool):
            parts.append(f"attempt {attempt}/{max_attempts}")
        else:
            parts.append(f"attempt {attempt}")
    for key in ("error_status", "status"):
        status = data.get(key)
        if isinstance(status, int) and not isinstance(status, bool):
            parts.append(f"status {status}")
            break
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        parts.append(_sanitized_bridge_message(error))
    return ": ".join(parts) if parts else None


def _event_frame(
    *,
    kind: str,
    subtype: str | None = None,
    session_id: str | None = None,
    effective_model: str | None = None,
    request_id: str | None = None,
    semantic: bool = False,
    detail: str | None = None,
    item_id: str | None = None,
) -> None:
    frame: dict[str, object] = {
        "type": "event",
        "kind": kind,
        "semantic": semantic,
    }
    if subtype is not None:
        frame["subtype"] = subtype
    if detail is not None:
        frame["detail"] = detail[:MAX_METADATA_BYTES]
    if session_id is not None:
        frame["session_id"] = session_id
    if effective_model is not None:
        frame["effective_model"] = effective_model
    if request_id is not None:
        frame["request_id"] = request_id
    if item_id is not None:
        frame["item_id"] = item_id
    _write_frame(frame)


def _commercial_boundary_from_rate_limit(
    *,
    rate_type: str | None,
    status: str | None,
    overage_status: str | None,
    commercial_mode: str,
) -> bool:
    if commercial_mode in {"promotional-credit", "owner-paid"}:
        return False
    allowed = {"allowed", "allowed_warning"}
    return overage_status in allowed or (rate_type == "overage" and status in allowed)


def _read_call_allowed(
    input_data: dict[str, object],
    *,
    rules: list[str],
    worktree: Path,
) -> bool:
    raw_path = input_data.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return False
    try:
        requested = _resolve_scoped_path(raw_path, worktree, strict=True)
    except BridgeInputError:
        return False
    if not _inside_worktree(requested, worktree):
        return False
    for rule in rules:
        match = SCOPED_ALLOWED_TOOL_PATTERN.fullmatch(rule)
        if match is None or match.group(1) != "Read":
            continue
        try:
            root, recursive = _read_rule_root(match.group(2), worktree)
        except BridgeInputError:
            return False
        if requested == root:
            return True
        if recursive and _inside_worktree(requested, root):
            return True
    return False


def _bash_call_allowed(
    input_data: dict[str, object],
    *,
    rules: list[str],
    worktree: Path,
) -> bool:
    command = input_data.get("command")
    if not isinstance(command, str) or not command:
        return False
    for rule in rules:
        match = SCOPED_ALLOWED_TOOL_PATTERN.fullmatch(rule)
        if match is None or match.group(1) != "Bash" or match.group(2) != command:
            continue
        try:
            _hash_command_paths(command, worktree)
        except BridgeInputError:
            return False
        return True
    return False


def _safe_search_pattern(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _governed_read_call_allowed(
    tool_name: object,
    input_data: dict[str, object],
    *,
    worktree: Path,
    evidence_read_roots: tuple[Path, ...] = (),
) -> bool:
    if tool_name == "Read":
        raw_path = input_data.get("file_path")
    elif tool_name in {"Grep", "Glob"}:
        raw_path = input_data.get("path", ".")
        if tool_name == "Grep":
            if not isinstance(input_data.get("pattern"), str):
                return False
            glob = input_data.get("glob")
            if glob is not None and not _safe_search_pattern(glob):
                return False
        elif not _safe_search_pattern(input_data.get("pattern")):
            return False
    else:
        return False
    if not isinstance(raw_path, str) or not raw_path:
        return False
    try:
        requested = _resolve_scoped_path(raw_path, worktree, strict=True)
    except BridgeInputError:
        return False
    return _inside_worktree(requested, worktree) or any(
        _inside_worktree(requested, root) for root in evidence_read_roots
    )


def _record_governed_denial(
    tool_name: object,
    tool_input: object,
    *,
    worktree: Path,
    diagnostics: dict[str, int] | None,
) -> None:
    """Count denied checks without retaining paths or arbitrary tool payloads.

    Hook and permission callbacks may see the same call; these are check counts,
    not distinct SDK permission denials. This never participates in admission.
    """
    if diagnostics is None:
        return
    reason = "invalid-input"
    if isinstance(tool_input, dict) and tool_name in {"Read", "Grep", "Glob"}:
        raw_path = tool_input.get("file_path" if tool_name == "Read" else "path", ".")
        if isinstance(raw_path, str) and raw_path and "\x00" not in raw_path:
            try:
                requested = _resolve_scoped_path(raw_path, worktree)
                if not _inside_worktree(requested, worktree):
                    reason = "outside-root"
                else:
                    try:
                        requested.resolve(strict=True)
                    except FileNotFoundError:
                        reason = "missing-path"
                    except (OSError, RuntimeError):
                        reason = "unresolvable-path"
            except (BridgeInputError, ValueError):
                reason = "unresolvable-path"
    diagnostics[reason] = min(diagnostics.get(reason, 0) + 1, 10_000)


def _hook_decision(allowed: bool) -> "HookJSONOutput":
    if allowed:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "tool call is outside the declared read-only worktree scope"
            ),
        }
    }


def _packaging_permissions(
    exploration_hook: "HookCallback",
    exploration_permission: object,
) -> tuple["HookCallback", object]:
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    packaging_started = False
    permission = cast(Callable[..., Awaitable[object]], exploration_permission)

    async def authorize_packaging_or_exploration(
        input_data: "HookInput",
        tool_use_id: str | None,
        context: "HookContext",
    ) -> "HookJSONOutput":
        nonlocal packaging_started
        if input_data.get("tool_name") == STRUCTURED_OUTPUT_TOOL:
            # Latch before any await so an immediately following tool callback
            # cannot resume exploration while the SDK validates the result.
            packaging_started = True
            # This permits the SDK call only. ToolResultBlock/ResultMessage
            # processing remains the sole acceptance authority for its value.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }
        if packaging_started:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        PACKAGING_EXPLORATION_DENIAL_REASON
                    ),
                }
            }
        return await exploration_hook(input_data, tool_use_id, context)

    async def authorize_packaging_or_exploration_permission(
        tool_name: str,
        tool_input: dict[str, object],
        context: object,
    ) -> object:
        nonlocal packaging_started
        if tool_name == STRUCTURED_OUTPUT_TOOL:
            # Match the hook's pre-await latch so either SDK permission path
            # closes exploration before schema validation can retry.
            packaging_started = True
            return PermissionResultAllow()
        if packaging_started:
            return PermissionResultDeny(
                message=PACKAGING_EXPLORATION_DENIAL_REASON
            )
        return await permission(tool_name, tool_input, context)

    return (
        authorize_packaging_or_exploration,
        authorize_packaging_or_exploration_permission,
    )


def _scoped_tool_hook(
    *,
    worktree: Path,
    allowed_tools: list[str],
) -> "HookCallback":
    async def deny_out_of_scope_tool(
        input_data: "HookInput",
        _tool_use_id: str | None,
        _context: "HookContext",
    ) -> "HookJSONOutput":
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input")
        allowed = False
        if isinstance(tool_input, dict):
            if tool_name == "Read":
                allowed = _read_call_allowed(
                    tool_input,
                    rules=allowed_tools,
                    worktree=worktree,
                )
            elif tool_name == "Bash":
                allowed = _bash_call_allowed(
                    tool_input,
                    rules=allowed_tools,
                    worktree=worktree,
                )
        return _hook_decision(allowed)

    return deny_out_of_scope_tool


def _governed_read_hook(
    *,
    worktree: Path,
    evidence_read_roots: tuple[Path, ...] = (),
    diagnostics: dict[str, int] | None = None,
) -> "HookCallback":
    async def authorize_governed_read(
        input_data: "HookInput",
        _tool_use_id: str | None,
        _context: "HookContext",
    ) -> "HookJSONOutput":
        tool_input = input_data.get("tool_input")
        allowed = isinstance(tool_input, dict) and _governed_read_call_allowed(
            input_data.get("tool_name"),
            tool_input,
            worktree=worktree,
            evidence_read_roots=evidence_read_roots,
        )
        if not allowed:
            _record_governed_denial(
                input_data.get("tool_name"),
                tool_input,
                worktree=worktree,
                diagnostics=diagnostics,
            )
        return _hook_decision(allowed)

    return authorize_governed_read


def _scoped_tool_permission(*, worktree: Path, allowed_tools: list[str]) -> object:
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    async def authorize_scoped_tool(
        tool_name: str,
        tool_input: dict[str, object],
        _context: object,
    ) -> object:
        allowed = False
        if tool_name == "Read":
            allowed = _read_call_allowed(
                tool_input,
                rules=allowed_tools,
                worktree=worktree,
            )
        elif tool_name == "Bash":
            allowed = _bash_call_allowed(
                tool_input,
                rules=allowed_tools,
                worktree=worktree,
            )
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message="tool call is outside the declared read-only worktree scope"
        )

    return authorize_scoped_tool


def _governed_read_permission(
    *,
    worktree: Path,
    evidence_read_roots: tuple[Path, ...] = (),
    diagnostics: dict[str, int] | None = None,
) -> object:
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    async def authorize_governed_read(
        tool_name: str,
        tool_input: dict[str, object],
        _context: object,
    ) -> object:
        if _governed_read_call_allowed(
            tool_name,
            tool_input,
            worktree=worktree,
            evidence_read_roots=evidence_read_roots,
        ):
            return PermissionResultAllow()
        _record_governed_denial(
            tool_name,
            tool_input,
            worktree=worktree,
            diagnostics=diagnostics,
        )
        return PermissionResultDeny(
            message="tool call is outside the declared read-only worktree scope"
        )

    return authorize_governed_read


async def _prompt_stream(prompt: str) -> AsyncIterator[dict[str, object]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": "",
    }


def _options(
    request: BridgeRequest, *, denial_diagnostics: dict[str, int] | None = None
) -> "ClaudeAgentOptions":
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    from claude_agent_sdk.types import HookEvent

    read_only = request["read_only"]
    assert isinstance(read_only, bool)
    if not read_only:
        raise BridgeInputError("Claude write execution is not an approved route")
    visible_tools, allowed_tools = validate_scoped_tool_configuration(
        cwd=request["cwd"],
        read_only=read_only,
        visible_tools=request.get("visible_tools"),
        allowed_tools=request.get("allowed_tools"),
    )
    explicit_scope = visible_tools is not None
    # Caller-declared evidence scopes are narrower than the default governed
    # worktree profile and therefore replace, rather than extend, its tools.
    worktree = Path(request["cwd"]).resolve(strict=True)
    evidence_read_roots = tuple(
        Path(root).resolve(strict=True)
        for root in request.get("evidence_read_roots", [])
    )
    permission_hook = (
        _scoped_tool_hook(worktree=worktree, allowed_tools=allowed_tools)
        if explicit_scope
        else _governed_read_hook(
            worktree=worktree,
            evidence_read_roots=evidence_read_roots,
            diagnostics=denial_diagnostics,
        )
    )
    permission_callback = (
        _scoped_tool_permission(worktree=worktree, allowed_tools=allowed_tools)
        if explicit_scope
        else _governed_read_permission(
            worktree=worktree,
            evidence_read_roots=evidence_read_roots,
            diagnostics=denial_diagnostics,
        )
    )
    hook_tools = visible_tools if explicit_scope else list(DEFAULT_READ_ONLY_TOOLS)
    assert hook_tools is not None
    output_schema = request.get("output_schema")
    if output_schema is not None:
        permission_hook, permission_callback = _packaging_permissions(
            permission_hook,
            permission_callback,
        )
        hook_tools = [*hook_tools, STRUCTURED_OUTPUT_TOOL]
    hooks: dict[HookEvent, list[HookMatcher]] = {
        "PreToolUse": [
            HookMatcher(
                matcher="|".join(map(re.escape, hook_tools)),
                hooks=[permission_hook],
            )
        ]
    }
    budget = get_settings().budget
    usd_ceiling = request["budget_usd"]
    if budget is not None:
        usd_ceiling = min(
            budget.max_usd,
            usd_ceiling if usd_ceiling is not None else budget.max_usd,
        )
    return ClaudeAgentOptions(
        model=request["requested_model"],
        effort=request["effort"],
        max_budget_usd=usd_ceiling,
        max_turns=budget.max_turns if budget is not None else None,
        task_budget={"total": budget.max_tokens} if budget is not None else None,
        cwd=request["cwd"],
        permission_mode="default",
        tools=visible_tools if explicit_scope else list(DEFAULT_READ_ONLY_TOOLS),
        allowed_tools=[],
        output_format=(
            {"type": "json_schema", "schema": output_schema}
            if output_schema is not None
            else None
        ),
        strict_mcp_config=True,
        mcp_servers={},
        setting_sources=[],
        skills=[],
        env=_claude_subscription_environment(),
        can_use_tool=permission_callback,
        hooks=hooks,
        include_partial_messages=False,
        include_hook_events=False,
        max_buffer_size=MAX_BRIDGE_LINE_BYTES,
        stderr=lambda _line: None,
        resume=request.get("resume_session_id"),
        cli_path=get_settings().claude_cli_path,
    )


def _strip_raw_api_from_process_env() -> None:
    """Remove raw API/gateway overrides from the bridge process environment."""
    for name in RAW_API_ENV_VARS | PYTHON_IMPORT_ENV_VARS:
        os.environ.pop(name, None)


def _codex_effort(effort: str) -> "ReasoningEffort":
    from openai_codex.generated.v2_all import ReasoningEffort

    mapped = "xhigh" if effort == "max" else effort
    return ReasoningEffort(mapped)


def _codex_sandbox(read_only: bool) -> "Sandbox":
    from openai_codex import Sandbox

    if os.environ.get(get_settings().env_name("OUTER_WORKER_SANDBOX")) == "1":
        return Sandbox.full_access
    return Sandbox.read_only if read_only else Sandbox.workspace_write


def _codex_sandbox_mode(request: BridgeRequest) -> str:
    """Return the effective-config spelling of the pinned SDK sandbox."""
    sandbox = _codex_sandbox(request["read_only"])
    return "danger-full-access" if sandbox.value == "full-access" else sandbox.value


def _codex_request_runtime_overrides(request: BridgeRequest) -> tuple[str, ...]:
    """Pin request-specific settings in the app-server's effective config."""
    return (
        *codex_runtime_overrides(),
        f"model={json.dumps(request['requested_model'])}",
        f"model_reasoning_effort={json.dumps(_codex_effort(request['effort']).value)}",
        f"sandbox_mode={json.dumps(_codex_sandbox_mode(request))}",
        'approval_policy="never"',
    )


def _codex_effective_config_is_closed(
    config: object, request: BridgeRequest
) -> bool:
    """Attest every effective key and leaf against the pinned allowlist."""
    from ._codex_config import exact_config, expected_config

    return exact_config(config, expected_config(_codex_request_runtime_overrides(request)))


def _enforce_codex_effective_config(codex: object, request: BridgeRequest) -> None:
    """Read and reject unsafe merged configuration before account or thread use."""
    try:
        from pydantic import BaseModel

        # The generated Config model can discard unknown nested members. Keep
        # raw JSON dictionaries so attestation sees every effective key.
        class EffectiveConfigResponse(BaseModel):
            config: dict[str, object]

        client = getattr(codex, "_client")
        response = client.request(
            "config/read",
            {"cwd": request["cwd"], "includeLayers": True},
            response_model=EffectiveConfigResponse,
        )
        config = response.config
    except Exception as exc:
        raise RuntimeError("unavailable") from exc
    if not _codex_effective_config_is_closed(config, request):
        raise RuntimeError("unavailable")


def _codex_thread_kwargs(request: BridgeRequest) -> CodexThreadKwargs:
    from openai_codex import ApprovalMode

    return {
        # The SDK must persist the first thread for normalized resume.  Live
        # conformance supplies a private suite-scoped CODEX_HOME that survives
        # both bridge launches and is scrubbed when the suite ends.
        "ephemeral": False,
        "model": request["requested_model"],
        "model_provider": CODEX_MODEL_PROVIDER,
        "service_tier": CODEX_SERVICE_TIER,
        "sandbox": _codex_sandbox(request["read_only"]),
        "approval_mode": ApprovalMode.deny_all,
        "cwd": request["cwd"],
    }


def _codex_start_or_resume_thread(
    client: object,
    request: BridgeRequest,
    thread_kwargs: CodexThreadKwargs,
) -> object:
    """Select the SDK resume call while preserving the same persisted home."""
    resume_session_id = request.get("resume_session_id")
    if resume_session_id is None:
        return client.thread_start(**thread_kwargs)  # type: ignore[attr-defined]
    resumed_kwargs = dict(thread_kwargs)
    resumed_kwargs.pop("ephemeral")
    return client.thread_resume(  # type: ignore[attr-defined]
        resume_session_id, **resumed_kwargs
    )


def _codex_turn_kwargs(request: BridgeRequest) -> CodexTurnKwargs:
    from openai_codex import ApprovalMode

    return {
        "effort": _codex_effort(request["effort"]),
        "model": request["requested_model"],
        "service_tier": CODEX_SERVICE_TIER,
        "sandbox": _codex_sandbox(request["read_only"]),
        "approval_mode": ApprovalMode.deny_all,
        "cwd": request["cwd"],
        "output_schema": cast("dict[str, JsonValue]", request["output_schema"]),
    }


def _codex_budget_overrides() -> tuple[str, ...]:
    budget = get_settings().budget
    if budget is None:
        return ()
    # Codex exposes a single user turn through this bridge.  Its recognized
    # output_token_limit is the native token-side bound for that turn.
    return (f"output_token_limit={budget.max_tokens}",)


def _codex_bin_kwargs() -> dict[str, str]:
    path = get_settings().codex_cli_path
    return {"codex_bin": str(path)} if path is not None else {}


def _codex_account_type(account_response: object) -> str | None:
    dump = getattr(account_response, "model_dump", None)
    if not callable(dump):
        return None
    payload = dump()
    if not isinstance(payload, dict):
        return None
    account = payload.get("account")
    if not isinstance(account, dict):
        return None
    account_type = account.get("type")
    return account_type if isinstance(account_type, str) else None


def _codex_item_type(item: object) -> str | None:
    root = getattr(item, "root", item)
    type_name = type(root).__name__
    if type_name.endswith("ThreadItem"):
        return type_name[: -len("ThreadItem")]
    return _metadata(getattr(root, "type", None))


def _codex_usage_from_sdk(usage: object) -> dict[str, int] | None:
    if usage is None:
        return None
    dump = getattr(usage, "model_dump", None)
    payload = dump() if callable(dump) else usage
    if not isinstance(payload, dict):
        return None
    for key in ("total", "last"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return _usage_payload(nested)
    return _usage_payload(payload)


class CodexTurnErrorNotification(Exception):
    """A Codex app-server error notification, described without raw bodies."""

    def __init__(self, text: str, *, will_retry: bool) -> None:
        super().__init__(f"{text} (will retry)" if will_retry else text)


def _codex_turn_error_text(error: object) -> str:
    message = getattr(error, "message", None)
    parts: list[str] = []
    info = getattr(error, "codex_error_info", None)
    if info is not None:
        dump = getattr(info, "model_dump", None)
        try:
            summary = dump() if callable(dump) else info
        except Exception:  # pragma: no cover - defensive against SDK models
            summary = None
        if isinstance(summary, str):
            parts.append(summary)
        elif isinstance(summary, dict) and len(summary) == 1:
            key, value = next(iter(summary.items()))
            parts.append(key if value in (None, {}) else f"{key}={value!r}")
        elif isinstance(summary, dict) and summary:
            parts.append(",".join(sorted(str(key) for key in summary)))
    if isinstance(message, str) and message.strip():
        parts.append(" ".join(message.split()))
    return ": ".join(parts) if parts else "codex error notification"


def _enforce_codex_chatgpt_login(codex: object) -> None:
    account = getattr(codex, "account", None)
    if not callable(account):
        raise RuntimeError("startup")
    response = account()
    account_type = _codex_account_type(response)
    if account_type in {"apiKey", "amazonBedrock"}:
        raise RuntimeError("startup")
    if account_type != "chatgpt":
        raise RuntimeError("startup")


def _retain_codex_response_text(
    final_text: str | None,
    latest_text: str | None,
    item: object,
) -> tuple[str | None, str | None]:
    """Retain at most two bounded message strings from the Codex item stream."""
    from openai_codex.generated.v2_all import AgentMessageThreadItem

    root = getattr(item, "root", None)
    if not isinstance(root, AgentMessageThreadItem):
        return final_text, latest_text
    text = root.text
    if not isinstance(text, str) or not text:
        return final_text, latest_text
    if len(text.encode("utf-8")) > MAX_FINAL_OUTPUT_BYTES:
        raise BridgeInputError("Codex terminal output exceeds the safe limit")
    phase = getattr(root.phase, "value", root.phase)
    if phase == "final_answer":
        return text, latest_text
    return final_text, text


def _probe_claude_model_runtime(request: BridgeRequest) -> None:
    """Validate the SDK-selected bundled CLI without authentication or a turn."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk._cli_version import __cli_version__
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        options = ClaudeAgentOptions(
            model=request["requested_model"],
            cwd=request["cwd"],
            cli_path=get_settings().claude_cli_path,
        )
        transport = SubprocessCLITransport(prompt="", options=options)
        # connect() uses an explicit option verbatim and consults _find_cli()
        # only when no path was configured.  Mirror that selection without
        # spawning the CLI or starting a model thread.
        selected_cli = transport._cli_path
        if selected_cli is None:
            selected_cli = transport._find_cli()
        selected = Path(selected_cli).resolve(strict=True)
        bundled = (
            Path(__import__("claude_agent_sdk").__file__).resolve().parent
            / "_bundled"
            / ("claude.exe" if os.name == "nt" else "claude")
        ).resolve(strict=True)
        configured = get_settings().claude_cli_path
        expected = (
            configured.resolve(strict=True) if configured is not None else bundled
        )
        if selected != expected:
            raise RuntimeError("startup")
        version = tuple(int(part) for part in __cli_version__.split("."))
        if len(version) != 3:
            raise ValueError("version")
    except Exception:
        _write_frame(
            {"type": "readiness", "status": "failed", "failure": "model-unsupported"}
        )
        return
    minimum = CLAUDE_MODEL_MINIMUM_CLI_VERSION.get(request["requested_model"])
    # Explicit paths were attested through the parent's mandatory wrapper
    # before this bridge launch.  The SDK constant applies only to its bundle.
    if configured is None and minimum is not None and version < minimum:
        _write_frame(
            {"type": "readiness", "status": "failed", "failure": "model-unsupported"}
        )
        return
    _write_frame({"type": "readiness", "status": "ready"})


def _probe_codex_app_server(request: BridgeRequest) -> None:
    """Initialize the pinned app-server and read its account without a turn."""
    try:
        import openai_codex

        if openai_codex.__version__ != PINNED_CODEX_VERSION:
            _write_frame(
                {"type": "readiness", "status": "failed", "failure": "sdk-version"}
            )
            return
        from openai_codex import Codex, CodexConfig

        _strip_raw_api_from_process_env()
        with _codex_subscription_environment() as (auth_environment, auth_path):
            codex_environment = _codex_sdk_environment(
                auth_environment, auth_path
            )
            config = CodexConfig(
                **_codex_bin_kwargs(),
                # Codex 0.154 initializes directly from the highest-precedence
                # overrides.  The 0.147-era config-lock export was triggered by
                # thread/start, is no longer part of the app-server protocol,
                # and made a readiness check create a thread unnecessarily.
                config_overrides=_codex_request_runtime_overrides(request),
                # Account readiness is independent of a project.  Keeping both
                # process cwd and CODEX_HOME in the private credential home
                # prevents repository configuration discovery during initialize.
                cwd=str(auth_path.parent),
                env=codex_environment,
            )
            with Codex(config) as codex:
                _enforce_codex_effective_config(codex, request)
                _enforce_codex_chatgpt_login(codex)
                if auth_path is not None:
                    auth_path.unlink(missing_ok=True)
    except Exception:
        # SDK exceptions can contain configuration or account details.  The
        # public probe contract retains only this fixed readiness class.
        _write_frame({"type": "readiness", "status": "failed", "failure": "bootstrap"})
        return
    _write_frame({"type": "readiness", "status": "ready"})


def _run_codex(
    request: BridgeRequest,
    *,
    reporter: _ProgressReporter | None = None,
) -> None:
    import openai_codex
    from openai_codex import Codex, CodexConfig
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        AgentMessageThreadItem,
        CollabAgentToolCallThreadItem,
        CommandExecutionThreadItem,
        DynamicToolCallThreadItem,
        ErrorNotification,
        FileChangeThreadItem,
        HookPromptThreadItem,
        ImageGenerationThreadItem,
        ImageViewThreadItem,
        ItemCompletedNotification,
        ItemGuardianApprovalReviewStartedNotification,
        ItemStartedNotification,
        McpToolCallThreadItem,
        SleepThreadItem,
        SubAgentActivityThreadItem,
        ThreadStartedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
        TurnStartedNotification,
        WebSearchThreadItem,
    )
    from openai_codex.models import Notification

    _strip_raw_api_from_process_env()
    if openai_codex.__version__ != PINNED_CODEX_VERSION:
        raise RuntimeError("startup")
    saw_message = False
    saw_terminal = False
    session_id: str | None = None
    request_id: str | None = None
    # The app-server accepts the requested model but its thread/turn protocol
    # does not currently report the effective model.  Preserve that distinction
    # instead of echoing the request as observed vendor evidence.
    effective_model: str | None = None
    usage_payload: dict[str, int] | None = None
    final_response_text: str | None = None
    latest_response_text: str | None = None
    turn_failed = False
    turn_failure: dict[str, str] | None = None
    tool_item_types = (
        CollabAgentToolCallThreadItem,
        CommandExecutionThreadItem,
        DynamicToolCallThreadItem,
        FileChangeThreadItem,
        HookPromptThreadItem,
        ImageGenerationThreadItem,
        ImageViewThreadItem,
        McpToolCallThreadItem,
        SleepThreadItem,
        SubAgentActivityThreadItem,
        WebSearchThreadItem,
    )

    with _codex_subscription_environment() as (auth_environment, auth_path):
        # A turn is never allowed to fall back to the operator's Codex login or
        # configuration. Login-status probing remains available without a
        # descriptor, but the SDK turn itself requires brokered credentials.
        codex_environment = _codex_sdk_environment(auth_environment, auth_path)
        config = CodexConfig(
            **_codex_bin_kwargs(),
            # Codex 0.154 removed the 0.147 debug.config_lockfile export. The
            # restrictive overrides are therefore applied directly and the
            # merged effective config is attested below before a thread exists.
            # The private CODEX_HOME still holds no project trust entry.
            config_overrides=_codex_request_runtime_overrides(request),
            cwd=request["cwd"],
            env=codex_environment,
        )
        with Codex(config) as codex:
            _enforce_codex_effective_config(codex, request)
            _enforce_codex_chatgpt_login(codex)
            if auth_path is not None:
                auth_path.unlink(missing_ok=True)
            thread_kwargs = _codex_thread_kwargs(request)
            thread = _codex_start_or_resume_thread(codex, request, thread_kwargs)
            session_id = _metadata(getattr(thread, "id", None))
            _event_frame(
                kind="system",
                subtype="thread_started",
                session_id=session_id,
                effective_model=effective_model,
                semantic=False,
            )
            turn = thread.turn(request["prompt"], **_codex_turn_kwargs(request))
            request_id = _metadata(getattr(turn, "id", None))
            _event_frame(
                kind="system",
                subtype="turn_started",
                session_id=session_id,
                request_id=request_id,
                effective_model=effective_model,
                semantic=False,
            )
            stream = turn.stream()
            shadow_reads: dict[tuple[str, str, str], tuple[str, str]] = {}
            _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
            try:
                for event in stream:
                    if not isinstance(event, Notification):
                        continue
                    payload = event.payload
                    if isinstance(payload, ThreadStartedNotification):
                        _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        session_id = _metadata(payload.thread.id) or session_id
                        _event_frame(
                            kind="system",
                            subtype="thread_started",
                            session_id=session_id,
                            effective_model=effective_model,
                            semantic=False,
                        )
                        continue
                    if isinstance(payload, TurnStartedNotification):
                        _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        session_id = _metadata(payload.thread_id) or session_id
                        request_id = _metadata(payload.turn.id) or request_id
                        _event_frame(
                            kind="system",
                            subtype="turn_started",
                            session_id=session_id,
                            request_id=request_id,
                            effective_model=effective_model,
                            semantic=False,
                        )
                        continue
                    if isinstance(payload, AgentMessageDeltaNotification):
                        _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        saw_message = True
                        session_id = _metadata(payload.thread_id) or session_id
                        request_id = _metadata(payload.turn_id) or request_id
                        _event_frame(
                            kind="stream",
                            subtype="agent_message_delta",
                            session_id=session_id,
                            request_id=request_id,
                            effective_model=effective_model,
                            semantic=True,
                        )
                        continue
                    if isinstance(payload, ItemStartedNotification):
                        item_root = payload.item.root
                        if isinstance(item_root, CommandExecutionThreadItem):
                            read = _codex_shadow_read(item_root)
                            if read is not None and len(shadow_reads) < 128:
                                shadow_reads[(payload.thread_id, payload.turn_id, item_root.id)] = read
                        if isinstance(item_root, tool_item_types):
                            _observe_progress(
                                reporter,
                                RuntimePhase.TOOL_EXECUTION,
                                last_tool=_codex_tool_label(item_root),
                            )
                        else:
                            _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        item_type = _codex_item_type(payload.item)
                        semantic = isinstance(
                            payload.item.root,
                            (
                                AgentMessageThreadItem,
                                CommandExecutionThreadItem,
                                FileChangeThreadItem,
                            ),
                        )
                        saw_message = saw_message or semantic
                        session_id = _metadata(payload.thread_id) or session_id
                        request_id = _metadata(payload.turn_id) or request_id
                        _event_frame(
                            kind="item",
                            subtype=_metadata(item_type),
                            session_id=session_id,
                            request_id=request_id,
                            effective_model=effective_model,
                            semantic=semantic,
                        )
                        continue
                    if isinstance(payload, ItemCompletedNotification):
                        completed_root = payload.item.root
                        if isinstance(completed_root, CommandExecutionThreadItem):
                            _codex_command_completion(
                                completed_root,
                                started_read=shadow_reads.pop(
                                    (payload.thread_id, payload.turn_id, completed_root.id), None
                                ),
                            )
                        if isinstance(completed_root, tool_item_types):
                            _record_progress_activity(reporter)
                        else:
                            _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        final_response_text, latest_response_text = (
                            _retain_codex_response_text(
                                final_response_text,
                                latest_response_text,
                                payload.item,
                            )
                        )
                        item_type = _codex_item_type(payload.item)
                        semantic = isinstance(
                            payload.item.root,
                            (
                                AgentMessageThreadItem,
                                CommandExecutionThreadItem,
                                FileChangeThreadItem,
                            ),
                        )
                        saw_message = saw_message or semantic
                        if isinstance(payload.item.root, AgentMessageThreadItem):
                            _event_frame(
                                kind="assistant",
                                subtype="message",
                                session_id=_metadata(payload.thread_id) or session_id,
                                request_id=_metadata(payload.turn_id) or request_id,
                                effective_model=effective_model,
                                semantic=True,
                            )
                        else:
                            _event_frame(
                                kind="item",
                                subtype=_metadata(item_type),
                                session_id=_metadata(payload.thread_id) or session_id,
                                request_id=_metadata(payload.turn_id) or request_id,
                                effective_model=effective_model,
                                semantic=semantic,
                            )
                        continue
                    if isinstance(payload, ThreadTokenUsageUpdatedNotification):
                        _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        session_id = _metadata(payload.thread_id) or session_id
                        request_id = _metadata(payload.turn_id) or request_id
                        usage_payload = (
                            _codex_usage_from_sdk(payload.token_usage) or usage_payload
                        )
                        _event_frame(
                            kind="usage",
                            subtype="updated",
                            session_id=session_id,
                            request_id=request_id,
                            effective_model=effective_model,
                            semantic=False,
                        )
                        continue
                    if isinstance(
                        payload, ItemGuardianApprovalReviewStartedNotification
                    ):
                        _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        raise RuntimeError("protocol")
                    if isinstance(payload, ErrorNotification):
                        _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
                        error_text = _codex_turn_error_text(payload.error)
                        if payload.will_retry is True:
                            # The app-server reconnects transient stream
                            # failures itself; the turn is still live.  Keep
                            # a bounded, redacted reason and keep streaming.
                            _event_frame(
                                kind="system",
                                subtype="error_retry",
                                session_id=_metadata(payload.thread_id) or session_id,
                                request_id=_metadata(payload.turn_id) or request_id,
                                effective_model=effective_model,
                                semantic=False,
                                detail=_sanitized_bridge_message(error_text),
                            )
                            continue
                        raise RuntimeError("protocol") from CodexTurnErrorNotification(
                            error_text, will_retry=False
                        )
                    if isinstance(payload, TurnCompletedNotification):
                        _observe_progress(reporter, RuntimePhase.RESULT_PACKAGING)
                        saw_terminal = True
                        saw_message = True
                        session_id = _metadata(payload.thread_id) or session_id
                        request_id = _metadata(payload.turn.id) or request_id
                        turn_status = getattr(
                            payload.turn.status, "value", payload.turn.status
                        )
                        turn_failed = turn_status in {"failed", "interrupted"}
                        if turn_failed:
                            turn_error = getattr(payload.turn, "error", None)
                            turn_failure_message = _sanitized_bridge_message(
                                f"{turn_status}: "
                                + (
                                    _codex_turn_error_text(turn_error)
                                    if turn_error is not None
                                    else "no error detail"
                                )
                            )
                            turn_failure = {
                                "exception": "CodexTurnFailed",
                                "message": turn_failure_message,
                            }
                        break
            finally:
                close_stream = getattr(stream, "close", None)
                if callable(close_stream):
                    close_stream()

    if not saw_terminal:
        raise RuntimeError("protocol" if saw_message else "startup")

    _observe_progress(reporter, RuntimePhase.RESULT_PACKAGING)
    final_output = final_response_text or latest_response_text

    structured_output: dict[str, object] | None = None
    if not turn_failed:
        try:
            candidate = json.loads(final_output or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError("protocol") from exc
        if not isinstance(candidate, dict):
            raise RuntimeError("protocol")
        structured_output = candidate
    status = "failed" if turn_failed else "completed"
    reason = "process-exit" if turn_failed else "completed"
    frame: dict[str, object] = {
        "type": "result",
        "status": status,
        "terminal_reason": reason,
        "session_id": session_id,
    }
    if effective_model is not None:
        frame["effective_model"] = effective_model
    if request_id is not None:
        frame["request_id"] = request_id
    if structured_output is not None:
        frame["structured_output"] = structured_output
    if usage_payload is not None:
        frame["usage"] = usage_payload
    if turn_failure is not None:
        frame["diagnostics"] = turn_failure
    _write_frame(frame)


async def _run_claude(
    request: BridgeRequest,
    *,
    reporter: _ProgressReporter | None = None,
) -> None:
    from claude_agent_sdk import (
        AssistantMessage,
        RateLimitEvent,
        ResultMessage,
        StreamEvent,
        SystemMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    session_id: str | None = None
    effective_model: str | None = None
    request_id: str | None = None
    pending_structured_output: tuple[str, dict[str, object]] | None = None
    accepted_structured_output: dict[str, object] | None = None
    saw_message = False
    denial_diagnostics: dict[str, int] = {}

    def terminal_protocol_failure() -> None:
        _write_frame(
            {
                "type": "result",
                "status": "failed",
                "terminal_reason": "protocol-failure",
                "session_id": session_id,
            }
        )

    messages = query(
        prompt=_prompt_stream(request["prompt"]),
        options=_options(request, denial_diagnostics=denial_diagnostics),
    )
    _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
    async for message in messages:
        if isinstance(message, RateLimitEvent):
            _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
            info = message.rate_limit_info
            rate_type = _metadata(info.rate_limit_type)
            status = _metadata(info.status)
            overage_status = _metadata(info.overage_status)
            subtype = ":".join(
                value
                for value in (rate_type, status, overage_status)
                if value is not None
            )
            _event_frame(
                kind="rate_limit",
                subtype=subtype or None,
                session_id=_metadata(message.session_id),
                request_id=_metadata(message.uuid),
                semantic=False,
            )
            if _commercial_boundary_from_rate_limit(
                rate_type=rate_type,
                status=status,
                overage_status=overage_status,
                commercial_mode=request["commercial_mode"],
            ):
                _write_frame(
                    {
                        "type": "result",
                        "status": "failed",
                        "terminal_reason": "commercial-boundary",
                        "session_id": _metadata(message.session_id),
                    }
                )
                return
            continue
        if isinstance(message, AssistantMessage):
            progress_phase = RuntimePhase.MODEL_ACTIVE
            for block in message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                if block.name == "StructuredOutput":
                    progress_phase = RuntimePhase.RESULT_PACKAGING
                    break
                progress_phase = RuntimePhase.TOOL_EXECUTION
            tool_label = None
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name != "StructuredOutput":
                    tool_label = _claude_tool_label(block.name)
            _observe_progress(reporter, progress_phase, last_tool=tool_label)
            saw_message = True
            session_id = _metadata(message.session_id) or session_id
            effective_model = _metadata(message.model) or effective_model
            request_id = _metadata(message.message_id or message.uuid) or request_id
            for block in message.content:
                if (
                    isinstance(block, ToolUseBlock)
                    and block.name == "StructuredOutput"
                    and isinstance(block.input, dict)
                ):
                    try:
                        pending_structured_output = (
                            block.id,
                            _bounded_structured_output(
                                block.input,
                                vendor="Claude",
                            ),
                        )
                    except BridgeInputError:
                        terminal_protocol_failure()
                        return
            _event_frame(
                kind="assistant",
                subtype="error" if message.error else "message",
                session_id=session_id,
                effective_model=effective_model,
                request_id=request_id,
                semantic=True,
            )
            continue
        if isinstance(message, UserMessage):
            _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
            blocks = message.content if isinstance(message.content, list) else ()
            for block in blocks:
                if (
                    isinstance(block, ToolResultBlock)
                    and pending_structured_output is not None
                    and block.tool_use_id == pending_structured_output[0]
                ):
                    if block.is_error is not True:
                        accepted_structured_output = pending_structured_output[1]
                    pending_structured_output = None
            continue
        if isinstance(message, SystemMessage):
            _observe_progress(reporter, RuntimePhase.MODEL_ACTIVE)
            data = message.data if isinstance(message.data, dict) else {}
            session_id = _metadata(data.get("session_id")) or session_id
            effective_model = _metadata(data.get("model")) or effective_model
            request_id = _metadata(data.get("uuid")) or request_id
            _event_frame(
                kind="system",
                subtype=_metadata(message.subtype),
                session_id=session_id,
                effective_model=effective_model,
                request_id=request_id,
                semantic=False,
                detail=(
                    _claude_api_retry_detail(data)
                    if message.subtype == "api_retry"
                    else None
                ),
            )
            continue
        if isinstance(message, StreamEvent):
            progress_phase, tool_label = _claude_stream_progress(message.event)
            _observe_progress(
                reporter,
                progress_phase,
                last_tool=tool_label,
            )
            saw_message = True
            event_type = (
                _metadata(message.event.get("type"))
                if isinstance(message.event, dict)
                else None
            )
            session_id = _metadata(message.session_id) or session_id
            request_id = _metadata(message.uuid) or request_id
            _event_frame(
                kind="stream",
                subtype=event_type,
                session_id=session_id,
                request_id=request_id,
                semantic=True,
            )
            continue
        if isinstance(message, ResultMessage):
            _observe_progress(reporter, RuntimePhase.RESULT_PACKAGING)
            saw_message = True
            session_id = _metadata(message.session_id) or session_id
            request_id = _metadata(message.uuid) or request_id
            model_from_usage = _model_from_usage(message.model_usage)
            effective_model = effective_model or model_from_usage
            output = message.result
            if output is not None and not isinstance(output, str):
                output = None
            if isinstance(output, str):
                try:
                    output_size = len(output.encode("utf-8"))
                except UnicodeEncodeError:
                    terminal_protocol_failure()
                    return
                if output_size > MAX_FINAL_OUTPUT_BYTES:
                    terminal_protocol_failure()
                    return
            result_subtype = _metadata(message.subtype)
            budget_exhausted = (
                message.is_error and result_subtype == "error_max_budget_usd"
            )
            recovered_after_budget = (
                budget_exhausted and accepted_structured_output is not None
            )
            status = "failed" if message.is_error else "completed"
            structured_output = message.structured_output
            if isinstance(structured_output, dict):
                try:
                    structured_output = _bounded_structured_output(
                        structured_output,
                        vendor="Claude",
                    )
                except BridgeInputError:
                    terminal_protocol_failure()
                    return
            recovered_accepted_output = (
                not message.is_error
                and not isinstance(structured_output, dict)
                and accepted_structured_output is not None
            )
            if recovered_after_budget:
                status = "completed"
                reason = "budget-exhausted-after-result"
                structured_output = accepted_structured_output
            elif budget_exhausted:
                reason = "budget-exhausted"
                structured_output = None
            elif recovered_accepted_output:
                # Claude can acknowledge the StructuredOutput tool successfully
                # yet omit the same object from ResultMessage. The observed
                # successful tool result is the authoritative SDK acceptance
                # boundary, so preserve it instead of discarding a completed
                # governed result as a model-result failure.
                status = "completed"
                reason = "completed"
                structured_output = accepted_structured_output
            elif not message.is_error and not isinstance(structured_output, dict):
                status = "failed"
                reason = "model-result"
            else:
                reason = "process-exit" if message.is_error else "completed"
            frame: dict[str, object] = {
                "type": "result",
                "status": status,
                "terminal_reason": reason,
                "session_id": session_id,
            }
            if effective_model is not None:
                frame["effective_model"] = effective_model
            if request_id is not None:
                frame["request_id"] = request_id
            if isinstance(structured_output, dict):
                frame["structured_output"] = structured_output
            if recovered_accepted_output:
                frame["structured_output_recovery"] = "accepted-tool-result"
            frame["permission_check_denials"] = denial_diagnostics
            frame["subtype"] = result_subtype
            frame["stop_reason"] = _metadata(message.stop_reason)
            if isinstance(message.api_error_status, int):
                frame["api_error_status"] = message.api_error_status
            if isinstance(message.permission_denials, list):
                tool_names: list[str] = []
                for denial in message.permission_denials:
                    tool_name = (
                        denial.get("tool_name")
                        if isinstance(denial, dict)
                        else getattr(denial, "tool_name", None)
                    )
                    bounded = _metadata(tool_name)
                    if bounded is not None and bounded not in tool_names:
                        tool_names.append(bounded)
                frame["permission_denial_count"] = len(message.permission_denials)
                frame["permission_denial_tools"] = tool_names[:16]
            usage = _usage_payload(message.usage)
            if usage is not None:
                frame["usage"] = usage
            vendor_cost = message.total_cost_usd
            if (
                isinstance(vendor_cost, (int, float))
                and not isinstance(vendor_cost, bool)
                and 0 <= vendor_cost <= 1_000_000
                and math.isfinite(vendor_cost)
            ):
                frame["total_cost_usd"] = float(vendor_cost)
                frame["cost_source"] = "vendor"
            _write_frame(frame)
            return
    if not saw_message:
        raise RuntimeError("startup")
    raise RuntimeError("protocol")


async def _run(
    request: BridgeRequest,
    *,
    reporter: _ProgressReporter | None = None,
) -> None:
    if request["vendor"] == "claude-probe":
        await asyncio.to_thread(_probe_claude_model_runtime, request)
        return
    if request["vendor"] == "codex-probe":
        await asyncio.to_thread(_probe_codex_app_server, request)
        return
    if request["vendor"] == "codex":
        await asyncio.to_thread(_run_codex, request, reporter=reporter)
        return
    await _run_claude(request, reporter=reporter)


MAX_FAILURE_MESSAGE_CHARS = 200
_SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|eyJ[A-Za-z0-9_-]{16,}(?:\.[A-Za-z0-9_-]{4,})*"
    r"|(?i:bearer)\s+[A-Za-z0-9._-]{8,}"
    r"|[A-Za-z0-9_-]{40,}"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_FILESYSTEM_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9:/])(?:~|\.{1,2})?"
    r"/(?:[^\s/]+/)*[^\s/]+"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:\\|\\\\)[^\s]+"
)


def _sanitized_bridge_message(message: str) -> str:
    """Redact and bound an explicitly trusted bridge-owned message."""
    sanitized = " ".join(message.split())
    sanitized = _SECRET_PATTERN.sub("<redacted>", sanitized)
    sanitized = _FILESYSTEM_PATH_PATTERN.sub("<redacted>", sanitized)
    return sanitized[:MAX_FAILURE_MESSAGE_CHARS]


def _sanitized_failure(exc: BaseException) -> dict[str, str]:
    """Describe a startup failure without credential or account material.

    Internal ``RuntimeError("startup")`` sentinels carry no detail of their
    own, so the nearest chained cause is described instead. Messages are
    retained only for explicitly enumerated bridge-owned exception classes;
    vendor exceptions can embed raw stdout or stderr and expose only a class.
    """
    cause: BaseException = exc
    seen: set[int] = set()
    while (
        isinstance(cause, RuntimeError)
        and cause.args
        and cause.args[0] in {"startup", "protocol", "disconnect"}
        and id(cause) not in seen
    ):
        seen.add(id(cause))
        nested = cause.__cause__ or cause.__context__
        if nested is None:
            break
        cause = nested
    failure = {"exception": type(cause).__name__}
    if not isinstance(cause, (BridgeInputError, CodexTurnErrorNotification)):
        return failure
    try:
        message = _sanitized_bridge_message(str(cause))
    except Exception:  # pragma: no cover - defensive against exotic __str__
        message = ""
    if message:
        failure["message"] = message
    return failure


def _error_frame(reason: str, exc: BaseException | None = None) -> None:
    if reason not in {"startup", "protocol", "disconnect", "unavailable"}:
        reason = "protocol"
    frame: dict[str, object] = {"type": "error", "reason": reason}
    if exc is not None:
        frame["diagnostics"] = _sanitized_failure(exc)
    _write_frame(frame)


async def main() -> int:
    reporter = _open_progress_reporter_from_env()
    try:
        try:
            request = _read_request()
        except BridgeInputError:
            _error_frame("protocol")
            return 2
        try:
            await _run(request, reporter=reporter)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _error_frame("disconnect")
            return 130
        except RuntimeError as exc:
            reason = exc.args[0] if exc.args else "protocol"
            _error_frame(reason if isinstance(reason, str) else "protocol", exc)
            return 1
        except Exception as exc:
            # Never print raw exception text: SDK errors can contain prompts,
            # paths, account details, or vendor response bodies.  Only the
            # exception class and a redacted, bounded message are surfaced.
            _error_frame("startup", exc)
            return 1
        return 0
    finally:
        if reporter is not None:
            reporter.close()


def codex_refresh() -> int:
    if sys.argv[1:] not in ([], ["--codex-refresh"]):
        return 2
    auth_directory: TemporaryDirectory[str] | None = None
    auth_path: Path | None = None
    try:
        from openai_codex import Codex, CodexConfig

        output_value = os.environ.pop(
            get_settings().env_name("CODEX_REFRESH_OUTPUT"), None
        )
        auth_directory, auth_environment, auth_path = (
            _materialize_codex_subscription_auth()
        )
        environment = _filtered_environment()
        environment.update(auth_environment)
        config = CodexConfig(
            **_codex_bin_kwargs(),
            config_overrides=_codex_budget_overrides(),
            env=environment,
        )
        with Codex(config) as codex:
            response = codex.account(refresh_token=True)
        authenticated = response.account is not None
        if output_value is not None:
            if auth_path is None:
                raise BridgeInputError("provider credential descriptor is missing")
            output = Path(output_value)
            if not output.is_absolute() or output.parent != Path(
                os.environ["CODEX_HOME"]
            ):
                raise BridgeInputError("provider refresh output is invalid")
            payload = auth_path.read_bytes()
            if not payload or len(payload) > 1024 * 1024:
                raise BridgeInputError("provider credential is oversized")
            descriptor = os.open(
                output,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        return 1
    finally:
        if auth_path is not None:
            auth_path.unlink(missing_ok=True)
        if auth_directory is not None:
            auth_directory.cleanup()
    sys.stdout.write(
        json.dumps({"authenticated": authenticated}, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    with RuntimeSettings.from_environment().use():
        if sys.argv[1:] == ["--codex-refresh"]:
            raise SystemExit(codex_refresh())
        raise SystemExit(asyncio.run(main()))
