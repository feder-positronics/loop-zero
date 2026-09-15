"""Host renewal probes using the real Codex broker and native record shape."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from loopzero import cli, credential_seal
from loopzero.runners import codex


def login(expiry: int, refresh: str = "fixture-refresh-only-on-host") -> dict:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    token = f"fixture.{claims}.signature"
    return {
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": token, "id_token": "fixture-identity",
                   "refresh_token": refresh, "account_id": "fixture-account"},
        "last_refresh": "2026-09-15T00:00:00Z",
    }


def write_login(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(credential_seal, "_host_refresh_wrapper", lambda: lambda spec: spec.argv)
    return tmp_path / "auth.json", tmp_path / "snapshot.json"


def renew(source, output):
    return cli.main(["accounts", "renew", "ci", "--source", str(source), "--out", str(output)])


def test_renew_rotates_on_host_before_nightly_horizon(paths, monkeypatch, capsys):
    source, output = paths
    now = int(time.time())
    original = login(now + 3600)
    rotated = login(now + 10 * 86400, "fixture-rotated-host-only")
    write_login(source, original)
    calls = []

    def refresh(command, **kwargs):
        calls.append(command)
        descriptor = int(kwargs["env"]["LOOPZERO_CODEX_AUTH_FD"])
        assert json.loads(os.pread(descriptor, 1024 * 1024, 0)) == original
        write_login(Path(kwargs["env"]["LOOPZERO_CODEX_REFRESH_OUTPUT"]), rotated)
        return subprocess.CompletedProcess(command, 0, '{"authenticated":true}', "")

    real_broker = codex.codex_subscription_credential
    monkeypatch.setattr(credential_seal, "_broker_for", lambda vendor: lambda **kw: real_broker(
        **kw, run_refresh=refresh, refresh_command=("fixture-vendor-cli",)))
    assert renew(source, output) == 0
    assert len(calls) == 1
    assert json.loads(source.read_bytes()) == rotated
    sealed = json.loads(output.read_bytes())
    assert sealed["tokens"]["refresh_token"] == sealed["tokens"]["access_token"]
    assert b"fixture-rotated-host-only" not in output.read_bytes()
    assert b"fixture-refresh-only-on-host" not in output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "fixture" not in captured.out


def test_renew_fresh_login_does_not_refresh(paths, monkeypatch):
    source, output = paths
    original = login(int(time.time()) + 10 * 86400)
    write_login(source, original)
    monkeypatch.setattr(codex, "_refresh_credential", lambda *a, **kw: pytest.fail("unnecessary refresh"))
    assert renew(source, output) == 0
    assert json.loads(source.read_bytes()) == original


def test_renew_rejects_short_lived_access_only_source(paths, monkeypatch, capsys):
    source, output = paths
    payload = login(int(time.time()) + 3600)
    payload["tokens"]["refresh_token"] = payload["tokens"]["access_token"]
    write_login(source, payload)
    monkeypatch.setattr(codex, "_refresh_credential", lambda *a, **kw: pytest.fail("access-only refresh"))
    assert renew(source, output) == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert captured.err == "account renewal failed: CodexCredentialUnavailable\n"
    assert captured.out == ""


def test_renew_failure_is_content_free(paths, monkeypatch, capsys):
    source, output = paths
    write_login(source, login(int(time.time()) + 3600))

    def fail(*args, **kwargs):
        raise codex.CodexCredentialRefreshFailed("fixture-secret /private/identity")

    monkeypatch.setattr(codex, "_refresh_credential", fail)
    assert renew(source, output) == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert captured.err == "account renewal failed: CodexCredentialRefreshFailed\n"
    assert captured.out == ""


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "repository", "existing-output"])
def test_renew_refuses_unsafe_paths(paths, unsafe, capsys):
    source, output = paths
    write_login(source, login(int(time.time()) + 10 * 86400))
    if unsafe == "symlink":
        alias = source.with_name("alias")
        alias.symlink_to(source)
        source = alias
    elif unsafe == "hardlink":
        os.link(source, source.with_name("alias"))
    elif unsafe == "public":
        source.chmod(0o644)
    elif unsafe == "repository":
        (source.parent / ".git").mkdir()
        (source.parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    else:
        output.write_text("do-not-overwrite")
    assert renew(source, output) == 1
    if unsafe == "existing-output":
        assert output.read_text() == "do-not-overwrite"
    else:
        assert not output.exists()
    assert str(source) not in capsys.readouterr().err


@pytest.mark.parametrize("failure", [None, "seal", "upload"])
def test_timer_uploads_only_snapshot_and_removes_staging(tmp_path, failure):
    script = Path(__file__).resolve().parents[2] / "docs/examples/renew-codex-nightly.sh"
    assert script.is_file()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    source = tmp_path / "auth.json"
    write_login(source, login(int(time.time()) + 10 * 86400))
    received = tmp_path / "uploaded.json"
    sealer = tmp_path / "loopzero"
    sealer.write_text(
        "#!/bin/bash\necho fixture-secret >&2; exit 1\n" if failure == "seal" else
        f'#!/bin/bash\nexec "{sys.executable}" -m loopzero.cli "$@"\n'
    )
    sealer.chmod(0o700)
    uploader = tmp_path / "gh"
    uploader.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\n"
        "assert sys.argv[1:] == ['secret', 'set', 'TEST_SECRET', '--repo', 'fixture/repo', '--env', 'nightly-conformance']\n"
        f"pathlib.Path({str(received)!r}).write_bytes(sys.stdin.buffer.read())\n"
        f"print('fixture-secret')\nsys.exit({1 if failure == 'upload' else 0})\n"
    )
    uploader.chmod(0o700)
    result = subprocess.run(
        ["bash", str(script), str(sealer), str(source), str(uploader),
         "fixture/repo", "nightly-conformance", "TEST_SECRET"],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (0 if failure is None else 1)
    assert "fixture-secret" not in result.stdout + result.stderr
    assert list(runtime.iterdir()) == []
    if failure == "seal":
        assert not received.exists()
    else:
        payload = json.loads(received.read_bytes())
        assert payload["tokens"]["refresh_token"] == payload["tokens"]["access_token"]
        assert b"fixture-refresh-only-on-host" not in received.read_bytes()
