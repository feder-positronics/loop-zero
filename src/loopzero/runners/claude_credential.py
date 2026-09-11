"""Trusted host-side renewal and snapshotting for Claude subscription OAuth."""

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .claude import ELIGIBLE_SUBSCRIPTION_TYPES, filtered_claude_environment

MAX_CREDENTIAL_BYTES = 1024 * 1024
REFRESH_TIMEOUT_S = 30.0
PROCESS_REAP_TIMEOUT_S = 1.0
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


def _kill_and_reap_refresh_process_group(
    process: subprocess.Popen[str],
) -> None:
    """Kill the refresh group without waiting indefinitely on inherited pipes."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=PROCESS_REAP_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    try:
        process.wait(timeout=PROCESS_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass


def _run_refresh_process_group(
    command: Sequence[str],
    *,
    timeout: float,
    check: bool,
    capture_output: bool,
    text: bool,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the Claude refresh command as one bounded process group."""
    if check or not capture_output or not text:
        raise ValueError("Claude refresh runner contract is invalid")
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_and_reap_refresh_process_group(process)
        raise subprocess.TimeoutExpired(command, timeout) from exc
    except BaseException:
        if process.poll() is None:
            _kill_and_reap_refresh_process_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _refresh_credential(
    credential: _ValidatedCredential,
    *,
    now_ms: int,
    horizon_ms: int,
    claude_binary: Path,
    run_status: Callable[..., subprocess.CompletedProcess[str]],
) -> _ValidatedCredential:
    with tempfile.TemporaryDirectory(prefix=get_settings().temp_name("claude-renewal")) as directory:
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
            outcome = run_status(
                command,
                timeout=REFRESH_TIMEOUT_S,
                check=False,
                capture_output=True,
                text=True,
                env=_staged_refresh_environment(staging_home),
                cwd=staging_home,
            )
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
) -> Iterator[int]:
    """Prefer renewable OAuth; otherwise lend a validated access-only token."""
    from .claude_token import token_snapshot

    with ExitStack() as stack:
        try:
            descriptor = stack.enter_context(_oauth_subscription_credential(
                requested_runtime_s=requested_runtime_s,
                credential_path=credential_path,
                clock=clock,
                run_status=run_status,
                claude_binary=claude_binary,
            ))
        except ClaudeCredentialError:
            descriptor = token_snapshot()
            if descriptor is None:
                raise
            stack.callback(os.close, descriptor)
        yield descriptor
