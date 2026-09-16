"""Declared-account enforcement with synthetic native records, never live logins."""

import base64
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import minimal_workflow

from loopzero import config
from loopzero.runners import claude, codex, cursor, process
from loopzero.runners.contract import RuntimeRequest
from loopzero.runners.settings import RuntimeSettings


def accounts():
    return importlib.import_module("loopzero.runners.accounts")


def fixture(tmp_path, vendor="codex", marker="selected"):
    private = tmp_path / "source"
    private.mkdir(mode=0o700, exist_ok=True)
    expiry = int(time.time()) + 864000
    token = (
        "a."
        + base64.urlsafe_b64encode(json.dumps({"exp": expiry, "tag": marker}).encode())
        .decode()
        .rstrip("=")
        + ".s"
    )
    if vendor == "claude":
        payload = {
            "claudeAiOauth": {
                "accessToken": marker,
                "refreshToken": marker,
                "expiresAt": expiry * 1000,
                "refreshTokenExpiresAt": expiry * 1000,
                "scopes": sorted(claude.REQUIRED_SCOPES),
                "subscriptionType": "max",
            }
        }
    elif vendor == "codex":
        payload = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "last_refresh": "2026-09-16T00:00:00Z",
            "tokens": {
                "access_token": token,
                "refresh_token": token,
                "id_token": "opaque",
                "account_id": marker,
            },
        }
    else:
        payload = {"accessToken": token, "refreshToken": token}
    source = private / f"{vendor}-{marker}.json"
    source.write_text(json.dumps(payload))
    source.chmod(0o600)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    declarations = accounts().parse_accounts(
        {
            vendor: {
                "private-label": {"kind": "oauth-login", "credential_path": str(source)}
            }
        }
    )
    settings = RuntimeSettings(
        tooling_root=workspace,
        toolchain_interpreter=Path(sys.executable),
        state_root=str(tmp_path / "state"),
        accounts=declarations,
    )
    transport = {
        "claude": claude.CLAUDE_SDK_TRANSPORT,
        "codex": codex.CODEX_SDK_TRANSPORT,
        "cursor": "cursor/cli-stream-json",
    }[vendor]
    request = RuntimeRequest(
        vendor=vendor,
        transport=transport,
        requested_model="test",
        effort="low",
        prompt="test",
        cwd=workspace,
        timeout_s=5,
        read_only=True,
        attempt_id="original-attempt",
    )
    return settings, request, source, payload


def test_profile_preserves_absent_empty_and_rejects_unsupported_accounts(consumer):
    tmp_path = consumer
    workflow = tmp_path / "workflow.toml"
    workflow.write_text(minimal_workflow())
    assert RuntimeSettings.from_profile(config.load_profile(tmp_path)).accounts is None
    workflow.write_text(minimal_workflow() + "\n[accounts]\n")
    assert RuntimeSettings.from_profile(config.load_profile(tmp_path)).accounts == {}
    workflow.write_text(
        minimal_workflow()
        + '\n[accounts.codex.one]\nkind = "api-key"\ncredential_path = "/host/secret"\n'
    )
    with pytest.raises(config.ConfigError):
        config.load_profile(tmp_path)


@pytest.mark.parametrize("vendor", ["claude", "codex", "cursor"])
def test_direct_adapter_empty_policy_denies_before_process_or_ambient_auth(
    tmp_path, monkeypatch, vendor
):
    settings, request, _source, _payload = fixture(tmp_path, vendor)
    settings = replace(settings, accounts={})
    monkeypatch.setenv(settings.env_name(f"{vendor.upper()}_AUTH_FD"), "0")

    def forbidden(*_a, **_kw):
        pytest.fail("denied account reached process")

    with settings.use():
        adapter = {
            "claude": claude.ClaudeAdapter,
            "codex": codex.CodexAdapter,
            "cursor": cursor.CursorAdapter,
        }[vendor](run_cli=forbidden, run_probe=forbidden)
    for method in (adapter.probe, adapter.run):
        with pytest.raises(accounts().DeclaredAccountError):
            method(request)


@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_declared_cli_is_denied_without_fallback(tmp_path, vendor):
    settings, request, _source, _payload = fixture(tmp_path, vendor)
    with settings.use():
        adapter = (claude.ClaudeAdapter if vendor == "claude" else codex.CodexAdapter)()
    with pytest.raises(accounts().DeclaredAccountError):
        adapter.run(replace(request, transport=f"{vendor}/cli"))


@pytest.mark.parametrize("poison", ["missing", "unsafe", "wrong-kind", "symlink"])
def test_declared_source_failure_never_uses_ambient_descriptor(
    tmp_path, monkeypatch, poison
):
    settings, request, source, _payload = fixture(tmp_path)
    fd = os.open(source, os.O_RDONLY)
    monkeypatch.setenv(settings.env_name("CODEX_AUTH_FD"), str(fd))
    try:
        if poison == "missing":
            source.unlink()
        elif poison == "unsafe":
            source.chmod(0o644)
        elif poison == "wrong-kind":
            source.write_text('{"OPENAI_API_KEY":"synthetic"}')
        else:
            destination = source.with_suffix(".saved")
            source.rename(destination)
            source.symlink_to(destination)
        with settings.use(), pytest.raises(accounts().DeclaredAccountError):
            with accounts().credential_scope(settings, "codex", request):
                pytest.fail("unsafe source lent credential")
    finally:
        os.close(fd)


def test_scope_is_immutable_context_local_and_child_projection_hides_sources(tmp_path):
    settings, request, source, _payload = fixture(tmp_path)
    with pytest.raises(TypeError):
        settings.accounts["claude"] = settings.accounts["codex"]
    before = dict(os.environ)
    barrier = threading.Barrier(2)

    def worker(marker):
        local = tmp_path / marker
        local.mkdir()
        settings, request, source, payload = fixture(local, marker=marker)
        with settings.use(), accounts().credential_scope(settings, "codex", request):
            fd = accounts().credential_descriptor("codex")
            barrier.wait(timeout=5)
            assert json.loads(os.pread(fd, 10000, 0))["tokens"]["account_id"] == marker
            projection = json.loads(
                next(iter(accounts().get_settings().child_environment().values()))
            )
            assert str(source) not in json.dumps(projection)
            assert "private-label" not in json.dumps(projection)
            assert projection["declared_credential_vendor"] == "codex"
            return fd

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(worker, ["one", "two"]))
    assert len(results) == 2
    assert dict(os.environ) == before


def test_native_contained_launch_uses_fresh_fds_and_hides_other_sources(
    tmp_path, monkeypatch
):
    settings, request, source, payload = fixture(tmp_path)
    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap unavailable")
    test = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--unshare-user", "--", "/usr/bin/true"],
        capture_output=True,
    )
    if test.returncode:
        pytest.skip("user namespace unavailable")
    probe = request.cwd / "probe.py"
    probe.write_text("""import hashlib,json,os,sys
assert sys.argv[1:] == ["--require-brokered-credential"]
settings=json.loads(os.environ["LOOPZERO_RUNTIME_SETTINGS"])
assert settings["declared_credential_vendor"] == "codex"
assert "accounts" not in settings
assert "OPENAI_API_KEY" not in os.environ
fd=int(os.environ["LOOPZERO_CODEX_AUTH_FD"])
print(hashlib.sha256(os.read(fd,10000)).hexdigest())
""")
    settings = replace(
        settings, bridge_path=probe, toolchain_interpreter=Path("/usr/bin/python3")
    )

    def wrapper(spec):
        cmd = [
            bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--die-with-parent",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(request.cwd),
            str(request.cwd),
        ]
        for path in (spec.private_tmpdir, *spec.private_mounts):
            cmd += ["--bind", str(path), str(path)]
        return [*cmd, "--chdir", str(request.cwd), "--", *spec.argv]

    monkeypatch.setenv("OPENAI_API_KEY", "hostile")
    before = dict(os.environ)
    with settings.use(), accounts().credential_scope(settings, "codex", request):
        snapshot = accounts().credential_descriptor("codex")
        expected = hashlib.sha256(os.pread(snapshot, 10000, 0)).hexdigest()
        for _ in range(2):
            result = process.run_cli(
                settings.bridge_command(request.cwd),
                cwd=request.cwd,
                input_text="",
                timeout_s=5,
                env={
                    "PATH": "/usr/bin:/bin",
                    "OPENAI_API_KEY": "hostile",
                    "LOOPZERO_CODEX_AUTH_FD": "0",
                    "LOOPZERO_RUNTIME_SETTINGS": "{}",
                },
                sandbox_wrapper=wrapper,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == expected
            os.fstat(snapshot)
    assert dict(os.environ) == before
