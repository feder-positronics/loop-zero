"""Trusted host-side renewal and snapshotting for Codex ChatGPT auth."""

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import base64
import fcntl
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .process import filtered_child_environment

MAX_CREDENTIAL_BYTES = 1024 * 1024
REFRESH_TIMEOUT_S = 30.0
REFRESH_SAFETY_MARGIN_S = 5 * 60
BROKER_LOCK_NAME = DEFAULT_SETTINGS.lock_name("codex-refresh")
CODEX_AUTH_FD_ENV = DEFAULT_SETTINGS.env_name("CODEX_AUTH_FD")
CODEX_AUTH_STATE_ENV = DEFAULT_SETTINGS.env_name("CODEX_AUTH_STATE")


class CodexCredentialError(RuntimeError):
    """The host broker could not produce a safe Codex credential snapshot."""


class UnsafeCodexCredential(CodexCredentialError):
    """A Codex credential or broker-owned file violated the safety contract."""


class CodexCredentialUnavailable(CodexCredentialError):
    """The protected Codex credential or runtime is unavailable."""


class CodexCredentialRefreshTimeout(CodexCredentialError):
    """Codex did not finish the isolated refresh within its deadline."""


class CodexCredentialRevoked(CodexCredentialError):
    """Codex explicitly reported that the ChatGPT login is revoked."""


class CodexCredentialRefreshFailed(CodexCredentialError):
    """Codex could not refresh the credential, without proof of revocation."""


@dataclass(frozen=True, slots=True)
class _ValidatedCredential:
    payload: bytes
    expires_at_s: int
    account_id: str


def _jwt_expiry(token: str, *, name: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        raise UnsafeCodexCredential(f"Codex credential {name} is invalid")
    try:
        padding = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeCodexCredential(f"Codex credential {name} is invalid") from exc
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise UnsafeCodexCredential(f"Codex credential {name} is invalid")
    parsed = int(expiry)
    if parsed != expiry or parsed <= 0:
        raise UnsafeCodexCredential(f"Codex credential {name} is invalid")
    return parsed


def _validate_payload(payload: bytes) -> _ValidatedCredential:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeCodexCredential("Codex credential JSON is invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("auth_mode") != "chatgpt":
        raise UnsafeCodexCredential("Codex ChatGPT credential is missing")
    if decoded.get("OPENAI_API_KEY") not in (None, ""):
        raise UnsafeCodexCredential("Codex credential contains an API key")
    tokens = decoded.get("tokens")
    if not isinstance(tokens, dict):
        raise UnsafeCodexCredential("Codex token set is missing")
    for name in ("access_token", "id_token", "refresh_token", "account_id"):
        if not isinstance(tokens.get(name), str) or not tokens[name]:
            raise UnsafeCodexCredential(f"Codex credential {name} is invalid")
    return _ValidatedCredential(
        payload=payload,
        expires_at_s=_jwt_expiry(tokens["access_token"], name="access_token"),
        account_id=tokens["account_id"],
    )


def _sandbox_snapshot_payload(credential: _ValidatedCredential) -> bytes:
    """Remove the durable refresh capability from a runtime snapshot."""
    decoded = json.loads(credential.payload)
    tokens = decoded["tokens"]
    assert isinstance(tokens, dict)
    access_token = tokens["access_token"]
    assert isinstance(access_token, str)
    tokens["refresh_token"] = access_token
    return json.dumps(decoded, separators=(",", ":")).encode("utf-8")


def _read_credential(path: Path) -> _ValidatedCredential:
    if path.is_symlink():
        raise UnsafeCodexCredential("Codex credential is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CodexCredentialUnavailable("Codex credential is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 2
            or metadata.st_size > MAX_CREDENTIAL_BYTES
        ):
            raise UnsafeCodexCredential("Codex credential is unsafe")
        payload = os.pread(descriptor, metadata.st_size, 0)
        if len(payload) != metadata.st_size:
            raise UnsafeCodexCredential("Codex credential read was incomplete")
    finally:
        os.close(descriptor)
    return _validate_payload(payload)


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
        raise UnsafeCodexCredential("Codex renewal lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise UnsafeCodexCredential("Codex renewal lock is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise UnsafeCodexCredential("Codex renewal lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


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
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _refresh_environment(codex_home: Path) -> dict[str, str]:
    environment = filtered_child_environment()
    for name in tuple(environment):
        if name.startswith("CODEX_") or name in {
            "OPENAI_API_KEY",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        }:
            environment.pop(name, None)
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _status_authenticated(stdout: str) -> bool | None:
    try:
        status = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(status, dict) or not isinstance(
        status.get("authenticated"), bool
    ):
        return None
    return status["authenticated"]


def _run_refresh_process_group(
    command: Sequence[str],
    *,
    timeout: float,
    check: bool,
    capture_output: bool,
    text: bool,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run the refresh bridge as one killable process group."""
    if check or not capture_output or not text:
        raise ValueError("Codex refresh runner contract is invalid")
    process = subprocess.Popen(
        list(command),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout, stderr=stderr
        ) from exc
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _default_refresh_command() -> tuple[str, ...]:
    settings = get_settings()
    root = settings.tooling_root or Path.cwd()
    python = settings.interpreter(root)
    if not python.is_file():
        raise CodexCredentialUnavailable("Codex refresh runtime is unavailable")
    return (*settings.bridge_command(root), "--codex-refresh")


def _refresh_credential(
    credential: _ValidatedCredential,
    *,
    horizon_s: float,
    run_refresh: Callable[..., subprocess.CompletedProcess[str]],
    refresh_command: Sequence[str],
) -> _ValidatedCredential:
    with tempfile.TemporaryDirectory(prefix=get_settings().temp_name("codex-renewal")) as directory:
        staging_home = Path(directory)
        staging_home.chmod(0o700)
        staging_credential = staging_home / "auth.json"
        _write_private_file(staging_credential, credential.payload)
        command = list(refresh_command)
        try:
            outcome = run_refresh(
                command,
                timeout=REFRESH_TIMEOUT_S,
                check=False,
                capture_output=True,
                text=True,
                env={**_refresh_environment(staging_home), **get_settings().child_environment()},
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexCredentialRefreshTimeout(
                "Codex credential refresh timed out"
            ) from exc
        except OSError as exc:
            raise CodexCredentialRefreshFailed(
                "Codex credential refresh could not start"
            ) from exc
        authenticated = _status_authenticated(outcome.stdout)
        if authenticated is False:
            raise CodexCredentialRevoked("Codex ChatGPT login was revoked")
        if outcome.returncode != 0 or authenticated is not True:
            raise CodexCredentialRefreshFailed("Codex credential refresh failed")
        refreshed = _read_credential(staging_credential)
        if refreshed.account_id != credential.account_id:
            raise UnsafeCodexCredential(
                "Codex credential account changed during refresh"
            )
        if (
            refreshed.expires_at_s <= credential.expires_at_s
            or refreshed.expires_at_s <= horizon_s
        ):
            raise UnsafeCodexCredential(
                "Codex credential refresh did not advance expiry"
            )
        return refreshed


def _install_credential(path: Path, credential: _ValidatedCredential) -> None:
    parent = path.parent
    try:
        metadata = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise UnsafeCodexCredential(
            "Codex credential directory is unavailable"
        ) from exc
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o002
    ):
        raise UnsafeCodexCredential("Codex credential directory is unsafe")
    temporary = parent / f".auth-{uuid.uuid4().hex}.tmp"
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


def _snapshot_descriptor(payload: bytes) -> int:
    if hasattr(os, "memfd_create"):
        descriptor = os.memfd_create(
            get_settings().memfd_name("codex-auth"), os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
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
    writable, raw_path = tempfile.mkstemp(prefix=get_settings().temp_name("codex-auth"))
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
def codex_subscription_credential(
    *,
    requested_runtime_s: float,
    credential_path: Path | None = None,
    clock: Callable[[], float] = time.time,
    run_refresh: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    refresh_command: Sequence[str] | None = None,
) -> Iterator[int]:
    """Renew if needed, then lend one sealed snapshot for a provider command."""
    if requested_runtime_s <= 0:
        raise ValueError("requested_runtime_s must be positive")
    path = credential_path or Path.home() / ".codex" / "auth.json"
    with _renewal_lock(path.parent / get_settings().lock_name("codex-refresh")):
        now_s = clock()
        horizon_s = now_s + requested_runtime_s + REFRESH_SAFETY_MARGIN_S
        credential = _read_credential(path)
        if credential.expires_at_s <= horizon_s:
            refreshed = _refresh_credential(
                credential,
                horizon_s=horizon_s,
                run_refresh=run_refresh or _run_refresh_process_group,
                refresh_command=refresh_command or _default_refresh_command(),
            )
            if _read_credential(path).payload != credential.payload:
                raise CodexCredentialRefreshFailed(
                    "Codex credential changed during trusted refresh"
                )
            _install_credential(path, refreshed)
            credential = _read_credential(path)
            if credential.payload != refreshed.payload:
                raise UnsafeCodexCredential(
                    "Codex credential changed during trusted installation"
                )
        snapshot = _snapshot_descriptor(_sandbox_snapshot_payload(credential))
    try:
        yield snapshot
    finally:
        os.close(snapshot)
