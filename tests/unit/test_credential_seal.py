"""Focused tests for the trusted release-wheel credential sealing command."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

import pytest

from loopzero import credential_seal
from loopzero.runners.settings import get_settings


def _payload(vendor: str, secret: str) -> dict[str, object]:
    if vendor == "claude":
        return {
            "claudeAiOauth": {
                "accessToken": secret,
                "refreshToken": secret,
                "expiresAt": 2_000_000,
                "refreshTokenExpiresAt": 2_000_000,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    if vendor == "codex":
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": secret,
                "id_token": "identity",
                "refresh_token": secret,
                "account_id": "account",
            },
            "last_refresh": "2026-09-09T17:58:29Z",
        }
    return {"accessToken": secret, "refreshToken": secret}


@pytest.mark.parametrize("vendor", credential_seal.VENDORS)
def test_entry_point_writes_only_access_only_mode_0600_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    vendor: str,
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source.json"
    source.write_text('{"durable":"refresh-capability"}', encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "snapshot.json"
    access_secret = f"access-only-{vendor}"
    snapshot = json.dumps(_payload(vendor, access_secret)).encode()

    @contextmanager
    def broker(**kwargs):
        assert kwargs["credential_path"] == source
        assert kwargs["requested_runtime_s"] == credential_seal.SEALED_RUNTIME_S
        broker_snapshot = tmp_path / "broker-snapshot.json"
        broker_snapshot.write_bytes(snapshot)
        broker_snapshot.chmod(0o400)
        descriptor = os.open(broker_snapshot, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(credential_seal, "_broker_for", lambda selected: broker)
    monkeypatch.setattr(
        credential_seal, "_host_refresh_wrapper", lambda: (lambda spec: spec.argv)
    )

    assert credential_seal.main(
        [vendor, "--source", str(source), "--out", str(output)]
    ) == 0

    captured = capsys.readouterr()
    expected_source = "oauth-file" if vendor == "claude" else f"{vendor}-auth-file"
    assert captured.out == f"credential source: {expected_source}\n"
    assert captured.err == ""
    assert json.loads(output.read_bytes()) == _payload(vendor, access_secret)
    assert b"refresh-capability" not in output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_entry_point_can_seal_from_normal_host_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path.chmod(0o700)
    output = tmp_path / "snapshot.json"
    token = "sk-ant-oat01-" + "x" * 80
    discovered = {
        "claudeCodeOauthToken": token,
        "source": "token-file(default)",
    }
    snapshot = json.dumps(discovered).encode()
    discovery_root = tmp_path / "host-state"
    monkeypatch.setenv("LOOPZERO_LIVE_STATE_ROOT", str(discovery_root))

    @contextmanager
    def broker(**kwargs):
        assert "credential_path" not in kwargs
        assert kwargs["allow_token_fallback"] is True
        assert get_settings().state_root == str(discovery_root)
        broker_snapshot = tmp_path / "broker-snapshot.json"
        broker_snapshot.write_bytes(snapshot)
        descriptor = os.open(broker_snapshot, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(credential_seal, "_broker_for", lambda _vendor: broker)
    monkeypatch.setattr(
        credential_seal, "_host_refresh_wrapper", lambda: (lambda spec: spec.argv)
    )

    assert credential_seal.main(["claude", "--out", str(output)]) == 0
    assert json.loads(output.read_bytes()) == discovered
    captured = capsys.readouterr()
    assert captured.out == "credential source: token-file(default)\n"
    assert captured.err == ""


@pytest.mark.parametrize("suffix", ["", "\n"], ids=["no-newline", "newline"])
def test_entry_point_accepts_an_explicit_raw_claude_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    tmp_path.chmod(0o700)
    token = "sk-ant-oat01-canary_" + "x" * 73
    source = tmp_path / ".credentials.json"
    source.write_text(token + suffix, encoding="ascii")
    source.chmod(0o600)
    output = tmp_path / "snapshot.json"
    monkeypatch.setattr(
        credential_seal.claude, "_validate_remote_token", lambda _: None
    )
    monkeypatch.setattr(
        credential_seal, "_host_refresh_wrapper", lambda: (lambda spec: spec.argv)
    )

    assert credential_seal.main(
        ["claude", "--source", str(source), "--out", str(output)]
    ) == 0
    assert json.loads(output.read_bytes()) == {
        "claudeCodeOauthToken": token,
        "source": "setup-token-file",
    }
    assert set(json.loads(output.read_bytes())) == {
        "claudeCodeOauthToken",
        "source",
    }
    assert b"refresh" not in output.read_bytes().lower()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert {path.name for path in tmp_path.iterdir()} == {
        source.name,
        output.name,
    }
    captured = capsys.readouterr()
    assert captured.out == "credential source: setup-token-file\n"
    assert captured.err == ""
    assert token not in captured.out + captured.err


def test_entry_point_keeps_explicit_claude_oauth_source_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "claude-token"
    oauth = {
        "claudeAiOauth": {
            "accessToken": "oauth-access",
            "refreshToken": "oauth-refresh",
            "expiresAt": 10**15,
            "refreshTokenExpiresAt": 10**15,
            "scopes": [
                "user:inference",
                "user:profile",
                "user:sessions:claude_code",
            ],
            "subscriptionType": "max",
        }
    }
    source.write_text(json.dumps(oauth), encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "snapshot.json"
    monkeypatch.setattr(
        credential_seal, "_host_refresh_wrapper", lambda: (lambda spec: spec.argv)
    )

    assert credential_seal.main(
        ["claude", "--source", str(source), "--out", str(output)]
    ) == 0

    sealed = json.loads(output.read_bytes())
    assert sealed == {
        "claudeAiOauth": {
            **oauth["claudeAiOauth"],
            "refreshToken": "oauth-access",
        }
    }
    captured = capsys.readouterr()
    assert captured.out == "credential source: oauth-file\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("contents", "mode", "error_class"),
    [
        (
            json.dumps(
                {
                    "claudeAiOauth": {"accessToken": "oauth-access"},
                    "claudeCodeOauthToken": "sk-ant-oat01-" + "x" * 80,
                }
            ),
            0o600,
            "UnsafeClaudeCredential",
        ),
        ("sk-ant-oat01-" + "x" * 80 + " ", 0o600, "UnsafeClaudeCredential"),
        ("sk-ant-oat01-" + "x" * 39, 0o600, "UnsafeClaudeCredential"),
        ("sk-ant-api03-" + "x" * 80, 0o600, "UnsafeClaudeCredential"),
        (
            json.dumps(
                {
                    "claudeCodeOauthToken": "sk-ant-oat01-" + "x" * 80,
                    "source": "setup-token-file",
                }
            ),
            0o600,
            "UnsafeClaudeCredential",
        ),
        ("sk-ant-oat01-" + "x" * 80, 0o644, "CredentialSealError"),
    ],
    ids=[
        "token-and-oauth",
        "trailing-space",
        "truncated-token",
        "api-key",
        "json-token-object",
        "world-readable-source",
    ],
)
def test_entry_point_rejects_non_setup_token_sources_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contents: str,
    mode: int,
    error_class: str,
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source"
    source.write_text(contents, encoding="ascii")
    source.chmod(mode)
    monkeypatch.setattr(
        credential_seal.claude,
        "_validate_remote_token",
        lambda _token: pytest.fail("invalid token reached remote validation"),
    )
    monkeypatch.setattr(
        credential_seal, "_host_refresh_wrapper", lambda: (lambda spec: spec.argv)
    )

    assert credential_seal.main(
        [
            "claude",
            "--source",
            str(source),
            "--out",
            str(tmp_path / "snapshot.json"),
        ]
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"credential sealing failed: {error_class}\n"
    assert contents not in captured.err
    assert not (tmp_path / "snapshot.json").exists()


def test_entry_point_rejects_unsafe_input_and_never_prints_credential(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source.json"
    secret = "must-never-be-printed"
    source.write_text(json.dumps({"secret": secret}), encoding="utf-8")
    source.chmod(0o644)

    assert credential_seal.main(
        ["codex", "--source", str(source), "--out", str(tmp_path / "out.json")]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err .startswith("credential sealing failed")
    assert secret not in captured.err


def test_entry_point_refuses_stdout_and_existing_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source.json"
    source.write_text('{"secret":"hidden"}', encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "snapshot.json"
    output.write_text("keep", encoding="utf-8")
    output.chmod(0o600)

    assert credential_seal.main(
        ["cursor", "--source", str(source), "--out", "-"]
    ) == 1
    assert credential_seal.main(
        ["cursor", "--source", str(source), "--out", str(output)]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hidden" not in captured.err
    assert output.read_text(encoding="utf-8") == "keep"


def test_entry_point_rejects_a_symlinked_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path.chmod(0o700)
    target = tmp_path / "target.json"
    target.write_text('{"secret":"hidden"}', encoding="utf-8")
    target.chmod(0o600)
    source = tmp_path / "source.json"
    source.symlink_to(target)

    assert credential_seal.main(
        ["cursor", "--source", str(source), "--out", str(tmp_path / "out.json")]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hidden" not in captured.err


def test_main_reports_only_a_fixed_error_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "secret-from-exception"
    monkeypatch.setattr(
        credential_seal,
        "seal_credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    assert credential_seal.main(
        ["claude", "--source", "/private/source", "--out", "/private/out"]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err .startswith("credential sealing failed")
    assert secret not in captured.err
