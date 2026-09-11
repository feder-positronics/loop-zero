"""Trusted host-side validation and snapshotting for Cursor browser auth."""

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
