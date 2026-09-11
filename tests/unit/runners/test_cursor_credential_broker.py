"""Focused tests for the trusted host-side Cursor credential snapshot."""

import base64
import json
import os
import stat
import sys
from pathlib import Path

import pytest


def _load_broker():
    from loopzero.runners import cursor as cursor_credential

    return cursor_credential


cursor_credential = _load_broker()


def _jwt(*, expires_at_s: int) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at_s}).encode()
    ).rstrip(b"=")
    return f"header.{encoded.decode()}.signature"


def _write_credential(path: Path, *, expires_at_s: int) -> dict[str, str]:
    payload = {
        "accessToken": _jwt(expires_at_s=expires_at_s),
        "refreshToken": "host-refresh-secret",
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return payload


def test_fresh_cursor_credential_is_snapshotted_for_requested_runtime(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".config" / "cursor" / "auth.json"
    payload = _write_credential(credential, expires_at_s=3_000)

    with cursor_credential.cursor_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
    ) as descriptor:
        snapshot = os.pread(descriptor, 1024 * 1024, 0)
        assert json.loads(snapshot) == {
            "accessToken": payload["accessToken"],
            "refreshToken": payload["accessToken"],
        }
        assert payload["refreshToken"].encode() not in snapshot
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o222 == 0

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_cursor_credential_must_cover_runtime_plus_safety_margin(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".config" / "cursor" / "auth.json"
    _write_credential(credential, expires_at_s=1_800)

    with pytest.raises(cursor_credential.CursorCredentialExpired):
        with cursor_credential.cursor_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
        ):
            pass


@pytest.mark.parametrize("unsafe_kind", ["mode", "symlink", "malformed"])
def test_cursor_credential_rejects_unsafe_or_invalid_files(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    credential = tmp_path / ".config" / "cursor" / "auth.json"
    credential.parent.mkdir(mode=0o700, parents=True)
    if unsafe_kind == "symlink":
        target = tmp_path / "elsewhere.json"
        _write_credential(target, expires_at_s=3_000)
        credential.symlink_to(target)
    elif unsafe_kind == "malformed":
        credential.write_text("{}", encoding="utf-8")
        credential.chmod(0o600)
    else:
        _write_credential(credential, expires_at_s=3_000)
        credential.chmod(0o644)

    with pytest.raises(cursor_credential.UnsafeCursorCredential):
        with cursor_credential.cursor_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
        ):
            pass
