"""Safe process execution shared by native CLI runtime adapters."""

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TextIO

from .contract import (
    MAX_PROTOCOL_LINE_BYTES,
    RuntimeProgressCallback,
    RuntimeProgressProtocolError,
    parse_runtime_progress_frame,
)
from .containment import worker_child_environment

LOGGER = logging.getLogger(__name__)
SandboxWrapper = Callable[[Sequence[str]], Sequence[str]]

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
RUNTIME_CACHE_ENV_VARS = frozenset(
    {
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
        "TMPDIR",
    }
)
OUTER_WORKER_SANDBOX_ENV = DEFAULT_SETTINGS.env_name("OUTER_WORKER_SANDBOX")


def merge_runtime_cache_environment(
    environment: Mapping[str, str],
    entries: Sequence[tuple[str, str]],
    *,
    write_root: Path,
) -> dict[str, str]:
    """Add only dispatcher-owned disposable cache variables to a child env."""
    merged = dict(environment)
    resolved_root = write_root.resolve()
    seen: set[str] = set()
    for name, value in entries:
        if name == get_settings().env_name("OUTER_WORKER_SANDBOX"):
            if name in seen or value != "1":
                raise ValueError(
                    "runtime capability profile contains an invalid environment"
                )
            seen.add(name)
            merged[name] = value
            continue
        candidate = Path(value)
        if (
            name in seen
            or name not in RUNTIME_CACHE_ENV_VARS
            or not value
            or not candidate.is_absolute()
            or not candidate.resolve().is_relative_to(resolved_root)
        ):
            raise ValueError(
                "runtime capability profile contains an invalid environment"
            )
        seen.add(name)
        merged[name] = value
    return merged


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
DEFAULT_STDOUT_LIMIT_BYTES = MAX_PROTOCOL_LINE_BYTES + 64 * 1024
DEFAULT_STDERR_LIMIT_BYTES = 256 * 1024
READ_CHUNK_CHARS = 64 * 1024
COLLECT_POLL_S = 0.05
RUNTIME_PROGRESS_FD_ENV_VAR = DEFAULT_SETTINGS.env_name("RUNTIME_PROGRESS_FD")
CODEX_AUTH_FD_ENV_VAR = DEFAULT_SETTINGS.env_name("CODEX_AUTH_FD")
MAX_PROGRESS_FRAME_BYTES = 512
MAX_PROGRESS_FRAMES = 1024
PROGRESS_FRAME_WINDOW_S = 1.0
PROGRESS_READ_CHUNK_BYTES = 4096
PROGRESS_READER_THREAD_NAME = "runtime-progress-reader"
PROC_ROOT = Path("/proc")
BOOT_ID_PATH = PROC_ROOT / "sys" / "kernel" / "random" / "boot_id"
UPTIME_PATH = PROC_ROOT / "uptime"


class ProcessGroupError(RuntimeError):
    """Raised when a child cannot be owned and reaped as one process group."""


class ProcessIdentityError(ProcessGroupError):
    """Raised when exact launch ownership cannot be captured or persisted."""


@dataclass(frozen=True, slots=True)
class ProcessLaunchIdentity:
    """Content-free kernel identity for one newly launched process group."""

    pid: int
    pgid: int
    boot_id: str
    start_ticks: int
    prelaunch_tick_lower_bound: int


ProcessLaunchCallback = Callable[[ProcessLaunchIdentity], None]
ProcessIdentityState = Literal["active", "gone", "unverifiable"]


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    """A process launched in its own session and therefore safe to terminate."""

    process: subprocess.Popen[str]
    pid: int
    pgid: int


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Collected child outcome without interpreting vendor output."""

    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    duration_s: float
    timed_out: bool
    output_limited: bool = False
    progress_diagnostic: bool = False


def _launch_identity_prerequisites() -> tuple[str, int]:
    """Sample immutable boot identity and a pre-launch kernel-tick bound."""
    try:
        boot_id = BOOT_ID_PATH.read_text(encoding="ascii").strip()
        uptime_s = float(UPTIME_PATH.read_text(encoding="ascii").split()[0])
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
    except (IndexError, OSError, TypeError, ValueError) as exc:
        raise ProcessIdentityError("runtime launch identity is unavailable") from exc
    if not boot_id or uptime_s < 0 or ticks_per_second <= 0:
        raise ProcessIdentityError("runtime launch identity is unavailable")
    return boot_id, int(uptime_s * ticks_per_second)


def _capture_launch_identity(
    pid: int, *, boot_id: str, prelaunch_tick_lower_bound: int
) -> ProcessLaunchIdentity:
    """Capture the exact child identity without reading command arguments."""
    try:
        stat_text = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8")
        fields = stat_text.rsplit(")", maxsplit=1)[1].split()
        start_ticks = int(fields[19])
        pgid = os.getpgid(pid)
    except (
        FileNotFoundError,
        IndexError,
        OSError,
        ProcessLookupError,
        ValueError,
    ) as exc:
        raise ProcessIdentityError("runtime launch identity is unavailable") from exc
    if pid <= 0 or pgid != pid or start_ticks < prelaunch_tick_lower_bound - 1:
        raise ProcessIdentityError("runtime launch identity is unavailable")
    return ProcessLaunchIdentity(
        pid=pid,
        pgid=pgid,
        boot_id=boot_id,
        start_ticks=start_ticks,
        prelaunch_tick_lower_bound=prelaunch_tick_lower_bound,
    )


def _read_proc_identity(pid: int, *, proc_root: Path) -> tuple[str, int, int] | None:
    try:
        stat_text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        fields = stat_text.rsplit(")", maxsplit=1)[1].split()
        return fields[0], int(fields[2]), int(fields[19])
    except FileNotFoundError:
        return None
    except (IndexError, OSError, ValueError) as exc:
        raise ProcessIdentityError("process identity evidence is unreadable") from exc


def classify_process_identity(
    identity: ProcessLaunchIdentity,
    *,
    proc_root: Path = PROC_ROOT,
    boot_id_path: Path = BOOT_ID_PATH,
    require_group_gone: bool = True,
) -> ProcessIdentityState:
    """Revalidate one process identity, and its owned group when requested."""
    try:
        current_boot_id = boot_id_path.read_text(encoding="ascii").strip()
    except OSError:
        return "unverifiable"
    if not current_boot_id:
        return "unverifiable"
    if current_boot_id != identity.boot_id:
        return "gone"
    try:
        owner = _read_proc_identity(identity.pid, proc_root=proc_root)
    except ProcessIdentityError:
        return "unverifiable"
    if owner is not None:
        state, pgid, start_ticks = owner
        if pgid != identity.pgid or start_ticks != identity.start_ticks:
            # The PID was reused, so this is not the recorded process.  For a
            # controller identity that is sufficient gone evidence.  For a
            # worker leader, keep scanning the original process group below;
            # a surviving child still makes settlement unverifiable.
            owner = None
            if not require_group_gone:
                return "gone"
        elif state != "Z":
            return "active"
    if not require_group_gone:
        return "gone"
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return "unverifiable"
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            candidate = _read_proc_identity(int(entry.name), proc_root=proc_root)
        except ProcessIdentityError:
            return "unverifiable"
        if (
            candidate is not None
            and candidate[0] != "Z"
            and candidate[1] == identity.pgid
        ):
            return "unverifiable"
    return "gone"


def current_process_identity() -> ProcessLaunchIdentity:
    """Capture the dispatcher process itself to close gaps between child launches."""
    boot_id, _tick_lower_bound = _launch_identity_prerequisites()
    pid = os.getpid()
    current = _read_proc_identity(pid, proc_root=PROC_ROOT)
    if current is None:
        raise ProcessIdentityError("dispatcher process identity is unavailable")
    _state, pgid, start_ticks = current
    return ProcessLaunchIdentity(
        pid=pid,
        pgid=pgid,
        boot_id=boot_id,
        start_ticks=start_ticks,
        prelaunch_tick_lower_bound=start_ticks,
    )


@dataclass(slots=True)
class _BoundedTextBuffer:
    """Thread-safe text collector with an aggregate UTF-8 byte ceiling."""

    limit_bytes: int
    chunks: list[str] = field(default_factory=list)
    size_bytes: int = 0

    def append(self, chunk: str) -> bool:
        encoded_size = len(chunk.encode("utf-8"))
        remaining = self.limit_bytes - self.size_bytes
        if encoded_size <= remaining:
            self.chunks.append(chunk)
            self.size_bytes += encoded_size
            return True
        if remaining > 0:
            bounded = chunk.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
            self.chunks.append(bounded)
            self.size_bytes += len(bounded.encode("utf-8"))
        return False

    def text(self) -> str:
        return "".join(self.chunks)


def _drain_progress_pipe(
    read_fd: int,
    on_progress: RuntimeProgressCallback,
    diagnostic: threading.Event,
    stop: threading.Event,
) -> None:
    """Drain bounded progress JSONL independently from private process output.

    This reader is the sole closer of ``read_fd``. Teardown signals ``stop`` so
    the main thread never races a close against an in-flight ``os.read``.
    """
    buffer = bytearray()
    dropping_oversized_line = False
    callback_enabled = True
    frame_times: deque[float] = deque()

    def accept_line(line: bytes) -> None:
        nonlocal callback_enabled
        now = time.monotonic()
        window_start = now - PROGRESS_FRAME_WINDOW_S
        while frame_times and frame_times[0] <= window_start:
            frame_times.popleft()
        if len(frame_times) >= MAX_PROGRESS_FRAMES:
            diagnostic.set()
            return
        frame_times.append(now)
        if not line.strip():
            diagnostic.set()
            return
        if len(line) > MAX_PROGRESS_FRAME_BYTES:
            diagnostic.set()
            return
        try:
            decoded = json.loads(line)
            progress = parse_runtime_progress_frame(decoded)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RuntimeProgressProtocolError,
        ):
            diagnostic.set()
            return
        if not callback_enabled:
            return
        try:
            on_progress(progress)
        except BaseException:
            callback_enabled = False
            diagnostic.set()

    try:
        try:
            poller = select.poll()
            poller.register(read_fd, select.POLLIN)
        except (OSError, ValueError):
            diagnostic.set()
            return
        while not stop.is_set():
            try:
                ready = poller.poll(int(COLLECT_POLL_S * 1000))
            except (OSError, ValueError):
                diagnostic.set()
                return
            if stop.is_set():
                return
            if not ready:
                continue
            try:
                chunk = os.read(read_fd, PROGRESS_READ_CHUNK_BYTES)
            except OSError:
                diagnostic.set()
                return
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if dropping_oversized_line:
                    dropping_oversized_line = False
                    continue
                accept_line(line)
            if len(buffer) > MAX_PROGRESS_FRAME_BYTES:
                buffer.clear()
                dropping_oversized_line = True
                diagnostic.set()
        if buffer and not dropping_oversized_line and not stop.is_set():
            accept_line(bytes(buffer))
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


def filtered_child_environment(
    base: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment where raw API and endpoint overrides cannot leak in."""
    environment = dict(os.environ if base is None else base)
    if extra is not None:
        environment.update(extra)
    for name in RAW_API_ENV_VARS | PYTHON_IMPORT_ENV_VARS:
        environment.pop(name, None)
    if extra is None or get_settings().env_name("RUNTIME_PROGRESS_FD") not in extra:
        environment.pop(get_settings().env_name("RUNTIME_PROGRESS_FD"), None)
    return environment


def launch_cli(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    progress_fd: int | None = None,
    on_launch: ProcessLaunchCallback | None = None,
    sandbox_wrapper: SandboxWrapper | None = None,
    unsandboxed: bool = False,
    unsandboxed_reason: str | None = None,
) -> ProcessHandle:
    """Launch an allowlisted CLI child inside a caller-supplied sandbox.

    The wrapper receives the original argv and returns isolated argv (for
    example a closure over :func:`containment.worker_isolated_command`).  A
    caller may make an exceptional unsandboxed launch only by setting
    ``unsandboxed=True`` and supplying a nonempty reason, which is logged.
    """
    if os.name != "posix":
        raise ProcessGroupError("native runtime process groups require POSIX")
    if sandbox_wrapper is None:
        if not unsandboxed:
            raise ProcessGroupError("runtime launch requires a sandbox wrapper")
        if not isinstance(unsandboxed_reason, str) or not unsandboxed_reason.strip():
            raise ProcessGroupError("unsandboxed runtime launch requires a reason")
        LOGGER.warning("unsandboxed runtime launch: %s", unsandboxed_reason.strip())
        launch_command = list(command)
    else:
        if unsandboxed or unsandboxed_reason is not None:
            raise ProcessGroupError(
                "sandboxed runtime launch cannot also request an unsandboxed exception"
            )
        launch_command = list(sandbox_wrapper(tuple(command)))
        if not launch_command:
            raise ProcessGroupError("sandbox wrapper returned an empty command")
    child_markers = {"AGENT_DISPATCH_DEPTH": "1"}
    if (len(command) >= 3 and command[1] == "-I"
            and Path(command[2]).name == get_settings().bridge_path.name):
        child_markers.update(get_settings().child_environment())
    if progress_fd is not None:
        child_markers[get_settings().env_name("RUNTIME_PROGRESS_FD")] = str(progress_fd)
    identity_prerequisites = (
        _launch_identity_prerequisites() if on_launch is not None else None
    )
    process = subprocess.Popen(
        launch_command,
        cwd=cwd,
        env=worker_child_environment(
            filtered_child_environment(env, extra=child_markers),
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        pass_fds=tuple(pass_fds),
    )
    handle = ProcessHandle(process=process, pid=process.pid, pgid=process.pid)
    if on_launch is not None:
        assert identity_prerequisites is not None
        try:
            boot_id, tick_lower_bound = identity_prerequisites
            identity = _capture_launch_identity(
                process.pid,
                boot_id=boot_id,
                prelaunch_tick_lower_bound=tick_lower_bound,
            )
            on_launch(identity)
        except BaseException as exc:
            cancel_cli(handle)
            if isinstance(exc, ProcessIdentityError):
                raise
            raise ProcessIdentityError(
                "runtime launch identity could not be persisted"
            ) from exc
    return handle


def cancel_cli(handle: ProcessHandle, *, grace_s: float = 2.0) -> None:
    """Terminate then forcibly reap the exact process group launched by this module."""
    if os.name != "posix":
        raise ProcessGroupError("native runtime process groups require POSIX")
    # Once Popen has reaped the leader, its numeric PGID can be recycled by an
    # unrelated session. Never signal after that identity reservation is gone.
    if handle.process.returncode is not None:
        return
    try:
        os.killpg(handle.pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_s
    while _process_group_has_live_member(handle.pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.01, remaining))
    # The direct child may exit after SIGTERM while an SDK/CLI descendant keeps
    # the pipes open. Keep the leader unreaped until after this sweep so its
    # PID/PGID cannot be recycled underneath the signal.
    if _process_group_has_live_member(handle.pgid):
        try:
            os.killpg(handle.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    handle.process.wait()


def _process_group_has_live_member(pgid: int) -> bool:
    """Return conservatively whether a Linux process group still has live members."""
    try:
        entries = tuple(PROC_ROOT.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            candidate = _read_proc_identity(int(entry.name), proc_root=PROC_ROOT)
        except ProcessIdentityError:
            return True
        if candidate is not None and candidate[0] != "Z" and candidate[1] == pgid:
            return True
    return False


def _process_exited_without_reaping(process: subprocess.Popen[str]) -> bool:
    """Observe direct-child exit while retaining its PID/PGID reservation."""
    if process.returncode is not None:
        return True
    try:
        status = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError:
        # Another waiter already consumed the leader. Let Popen record that
        # loss so cancel_cli refuses to signal a potentially recycled PGID.
        process.poll()
        return True
    return status is not None


def run_cli(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    terminate_grace_s: float = 2.0,
    pass_fds: Sequence[int] = (),
    max_stdout_bytes: int = DEFAULT_STDOUT_LIMIT_BYTES,
    max_stderr_bytes: int = DEFAULT_STDERR_LIMIT_BYTES,
    on_progress: RuntimeProgressCallback | None = None,
    on_launch: ProcessLaunchCallback | None = None,
    sandbox_wrapper: SandboxWrapper | None = None,
    unsandboxed: bool = False,
    unsandboxed_reason: str | None = None,
) -> ProcessResult:
    """Run and collect one owned CLI with bounded pipes and group cleanup."""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if max_stdout_bytes <= 0 or max_stderr_bytes <= 0:
        raise ValueError("process output limits must be positive")
    started = time.monotonic()
    progress_read_fd: int | None = None
    progress_write_fd: int | None = None
    inherited_fds = tuple(pass_fds)
    owned_auth_fd: int | None = None
    raw_auth_fd = (env or {}).get(get_settings().env_name("CODEX_AUTH_FD"))
    if raw_auth_fd is not None:
        try:
            candidate_auth_fd = int(raw_auth_fd)
        except ValueError:
            candidate_auth_fd = -1
        if candidate_auth_fd in inherited_fds:
            owned_auth_fd = candidate_auth_fd
    try:
        if on_progress is not None:
            progress_read_fd, progress_write_fd = os.pipe()
            inherited_fds = (*inherited_fds, progress_write_fd)
        handle = launch_cli(
            command,
            cwd=cwd,
            env=env,
            pass_fds=inherited_fds,
            progress_fd=progress_write_fd,
            on_launch=on_launch,
            sandbox_wrapper=sandbox_wrapper,
            unsandboxed=unsandboxed,
            unsandboxed_reason=unsandboxed_reason,
        )
    except BaseException:
        for fd in (progress_read_fd, progress_write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        raise
    finally:
        if owned_auth_fd is not None:
            os.close(owned_auth_fd)
            if isinstance(env, MutableMapping):
                env.pop(get_settings().env_name("CODEX_AUTH_FD"), None)
    if progress_write_fd is not None:
        os.close(progress_write_fd)
    stdout_buffer = _BoundedTextBuffer(max_stdout_bytes)
    stderr_buffer = _BoundedTextBuffer(max_stderr_bytes)
    output_limited = threading.Event()
    progress_diagnostic = threading.Event()
    progress_stop = threading.Event()

    def read_stream(
        stream: TextIO,
        buffer: _BoundedTextBuffer,
    ) -> None:
        try:
            while True:
                chunk = stream.read(READ_CHUNK_CHARS)
                if not chunk:
                    return
                if not buffer.append(chunk):
                    output_limited.set()
                    return
        except (OSError, ValueError):
            return

    def write_stdin() -> None:
        stdin = handle.process.stdin
        if stdin is None:
            return
        try:
            stdin.write(input_text)
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    assert handle.process.stdout is not None
    assert handle.process.stderr is not None
    io_threads = (
        threading.Thread(
            target=read_stream,
            args=(handle.process.stdout, stdout_buffer),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(handle.process.stderr, stderr_buffer),
            daemon=True,
        ),
        threading.Thread(target=write_stdin, daemon=True),
    )
    progress_thread = (
        threading.Thread(
            target=_drain_progress_pipe,
            args=(progress_read_fd, on_progress, progress_diagnostic, progress_stop),
            name=PROGRESS_READER_THREAD_NAME,
            daemon=True,
        )
        if progress_read_fd is not None and on_progress is not None
        else None
    )
    threads = (
        *io_threads,
        *((progress_thread,) if progress_thread is not None else ()),
    )
    try:
        for thread in threads:
            thread.start()
        deadline = started + timeout_s
        timed_out = False
        while not _process_exited_without_reaping(handle.process):
            if output_limited.is_set():
                cancel_cli(handle, grace_s=terminate_grace_s)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                cancel_cli(handle, grace_s=terminate_grace_s)
                break
            time.sleep(min(COLLECT_POLL_S, remaining))
        else:
            # A successful direct child may still leave SDK/app-server
            # descendants after closing their inherited pipes. Reap them before
            # a same-vendor fallback can begin.
            cancel_cli(handle, grace_s=terminate_grace_s)

        for thread in threads:
            thread.join(timeout=terminate_grace_s)
        for stream in (handle.process.stdout, handle.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=terminate_grace_s)
        if progress_thread is not None and progress_thread.is_alive():
            progress_diagnostic.set()
        return ProcessResult(
            returncode=handle.process.returncode,
            stdout=stdout_buffer.text(),
            stderr=stderr_buffer.text(),
            duration_s=time.monotonic() - started,
            timed_out=timed_out,
            output_limited=output_limited.is_set(),
            progress_diagnostic=progress_diagnostic.is_set(),
        )
    except BaseException:
        cancel_cli(handle, grace_s=terminate_grace_s)
        raise
    finally:
        progress_stop.set()
        for thread in threads:
            thread.join(timeout=terminate_grace_s)
        if progress_thread is not None and progress_thread.is_alive():
            progress_diagnostic.set()
            progress_thread.join(timeout=terminate_grace_s)


def isolated_python_import_available(
    python: Path,
    module: str,
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    sandbox_wrapper: SandboxWrapper | None = None,
    unsandboxed: bool = False,
    unsandboxed_reason: str | None = None,
) -> bool:
    """Probe one trusted SDK import under owned, sterile Python isolation."""
    if not python.is_file() or not os.access(python, os.X_OK):
        return False
    if not module.isascii() or not module.isidentifier():
        raise ValueError("module must be a simple Python identifier")
    try:
        with TemporaryDirectory(prefix=get_settings().temp_name("sdk-probe")) as directory:
            result = run_cli(
                [str(python), "-I", "-c", f"import {module}"],
                cwd=Path(directory),
                input_text="",
                timeout_s=timeout_s,
                env=filtered_child_environment(env),
                max_stdout_bytes=64 * 1024,
                max_stderr_bytes=64 * 1024,
                sandbox_wrapper=sandbox_wrapper,
                unsandboxed=unsandboxed,
                unsandboxed_reason=unsandboxed_reason,
            )
    except (OSError, subprocess.TimeoutExpired, ProcessGroupError):
        return False
    return result.returncode == 0 and not result.timed_out and not result.output_limited
