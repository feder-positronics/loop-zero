"""Focused tests for the trusted release-wheel credential sealing command."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

import pytest

from loopzero import credential_seal


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
    assert captured.out == captured.err == ""
    assert json.loads(output.read_bytes()) == _payload(vendor, access_secret)
    assert b"refresh-capability" not in output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


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
    assert captured.err == "credential sealing failed\n"
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
    assert captured.err == "credential sealing failed\n"
    assert secret not in captured.err
