"""Focused tests for the trusted host-side Codex credential broker."""

import base64
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_broker():
    from loopzero.runners import codex as codex_credential

    return codex_credential


codex_credential = _load_broker()


def _test_bwrap_argv(
    command: list[str],
    *,
    tooling_root: Path,
    venv_root: Path,
    bridge_dir: Path,
    workspace_root: Path,
) -> list[str]:
    """Small test-only wrapper demonstrating the kernel/runner argv seam."""
    argv = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
    ]
    for root in (tooling_root, venv_root, bridge_dir):
        argv.extend(("--ro-bind", str(root), str(root)))
    argv.extend(("--bind", str(workspace_root), str(workspace_root)))
    return [*argv, "--", *command]


def _jwt(*, expires_at_s: int) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at_s}).encode()
    ).rstrip(b"=")
    return f"header.{encoded.decode()}.signature"


def _credential(*, expires_at_s: int, refresh_token: str = "refresh") -> dict:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _jwt(expires_at_s=expires_at_s),
            "id_token": _jwt(expires_at_s=expires_at_s),
            "refresh_token": refresh_token,
            "account_id": "account-1",
        },
        "last_refresh": "2026-08-31T00:00:00Z",
    }


def _write_credential(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _runtime_snapshot(payload: dict) -> dict:
    snapshot = json.loads(json.dumps(payload))
    snapshot["tokens"]["refresh_token"] = snapshot["tokens"]["access_token"]
    return snapshot


def test_fresh_codex_credential_is_snapshotted_without_refresh(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".codex" / "auth.json"
    payload = _credential(expires_at_s=3_000)
    _write_credential(credential, payload)

    def forbidden_refresh(*_args, **_kwargs):
        raise AssertionError("fresh credentials must not refresh")

    with codex_credential.codex_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        run_refresh=forbidden_refresh,
    ) as descriptor:
        snapshot = json.loads(os.pread(descriptor, 1024 * 1024, 0))
        assert snapshot == _runtime_snapshot(payload)
        assert snapshot["tokens"]["refresh_token"] != "refresh"


def test_near_expiry_codex_credential_refreshes_and_persists_rotated_token(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".codex" / "auth.json"
    original = _credential(expires_at_s=1_100, refresh_token="old-refresh")
    refreshed = _credential(expires_at_s=3_000, refresh_token="new-refresh")
    _write_credential(credential, original)
    observed: dict[str, object] = {}

    def refresh(command, *, env, **kwargs):
        observed["command"] = command
        observed["timeout"] = kwargs["timeout"]
        staging = Path(env["CODEX_HOME"]) / "auth.json"
        assert json.loads(staging.read_text(encoding="utf-8")) == original
        _write_credential(staging, refreshed)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"authenticated":true}',
            stderr="",
        )

    with codex_credential.codex_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        run_refresh=refresh,
        refresh_command=("trusted-python", "trusted-bridge", "trusted-codex"),
    ) as descriptor:
        assert json.loads(os.pread(descriptor, 1024 * 1024, 0)) == _runtime_snapshot(
            refreshed
        )

    assert json.loads(credential.read_text(encoding="utf-8")) == refreshed
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE(
            (credential.parent / codex_credential.BROKER_LOCK_NAME).stat().st_mode
        )
        == 0o600
    )
    assert observed == {
        "command": ["trusted-python", "trusted-bridge", "trusted-codex"],
        "timeout": codex_credential.REFRESH_TIMEOUT_S,
    }


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        ("timeout", "CodexCredentialRefreshTimeout"),
        ("revoked", "CodexCredentialRevoked"),
    ],
)
def test_codex_refresh_failure_is_typed_and_preserves_host_credential(
    tmp_path: Path,
    failure: str,
    error_type: str,
) -> None:
    credential = tmp_path / ".codex" / "auth.json"
    original = _credential(expires_at_s=1_100)
    _write_credential(credential, original)

    def refresh(command, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"authenticated":false}',
            stderr="private detail",
        )

    error = getattr(codex_credential, error_type)
    with pytest.raises(error):
        with codex_credential.codex_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_refresh=refresh,
            refresh_command=("trusted-python", "trusted-bridge", "trusted-codex"),
        ):
            pass

    assert json.loads(credential.read_text(encoding="utf-8")) == original


def test_codex_refresh_rejects_unadvanced_expiry(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".codex" / "auth.json"
    original = _credential(expires_at_s=1_100)
    _write_credential(credential, original)

    def refresh(command, *, env, **_kwargs):
        staging = Path(env["CODEX_HOME"]) / "auth.json"
        _write_credential(staging, _credential(expires_at_s=1_200))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"authenticated":true}',
            stderr="",
        )

    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_refresh=refresh,
            refresh_command=("trusted-python", "trusted-bridge", "trusted-codex"),
        ):
            pass

    assert json.loads(credential.read_text(encoding="utf-8")) == original


def test_default_refresh_runner_uses_contained_process_seam_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def contained_run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return codex_credential.ProcessResult(
            returncode=-9,
            stdout="bounded-out",
            stderr="bounded-error",
            duration_s=1.0,
            timed_out=True,
        )

    monkeypatch.setattr(codex_credential, "default_run_cli", contained_run)
    wrapper = lambda command: ["bwrap", "--", *command]

    with pytest.raises(subprocess.TimeoutExpired):
        codex_credential._run_refresh_process_group(
            ["bridge"],
            timeout=1,
            check=False,
            capture_output=True,
            text=True,
            env={},
            cwd=tmp_path,
            sandbox_wrapper=wrapper,
        )

    assert observed == {
        "command": ["bridge"],
        "cwd": tmp_path,
        "input_text": "",
        "timeout_s": 1,
        "env": {},
        "sandbox_wrapper": wrapper,
    }


def test_refresh_bridge_requests_an_explicit_managed_token_refresh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from loopzero.runners import bridge
    observed: dict[str, object] = {}

    class FakeCodex:
        def __init__(self, config):
            observed["config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def account(self, *, refresh_token):
            observed["refresh_token"] = refresh_token
            return SimpleNamespace(account=object())

    fake_sdk = ModuleType("openai_codex")
    fake_sdk.Codex = FakeCodex
    fake_sdk.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
    monkeypatch.setattr(bridge.sys, "argv", ["codex_refresh_bridge.py"])

    assert bridge.codex_refresh() == 0
    assert observed["refresh_token"] is True
    assert isinstance(observed["config"], dict)
    assert "codex_bin" not in observed["config"]
    assert json.loads(capsys.readouterr().out) == {"authenticated": True}


def test_codex_refresh_wrapper_binds_sdk_venv_bridge_and_workspace(
    tmp_path: Path,
) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap unavailable: cannot test the sandbox-wrapper contract")
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--", "/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        diagnostic = (probe.stderr or probe.stdout).strip().splitlines()
        detail = diagnostic[-1] if diagnostic else "unknown namespace error"
        pytest.skip(f"bubblewrap cannot create a namespace: {detail}")

    from loopzero.runners import process

    tooling_root = Path(__file__).resolve().parents[3]
    venv_root = Path(sys.prefix).resolve()
    bridge = Path(codex_credential.__file__).resolve().with_name("bridge.py")
    bridge_dir = bridge.parent
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    command = [sys.executable, "-I", str(bridge), "--codex-refresh"]

    def wrapper(argv):
        return _test_bwrap_argv(
            list(argv),
            tooling_root=tooling_root,
            venv_root=venv_root,
            bridge_dir=bridge_dir,
            workspace_root=workspace_root,
        )

    wrapped = wrapper(command)
    for mode, root in (
        ("--ro-bind", venv_root),
        ("--ro-bind", bridge_dir),
        ("--bind", workspace_root),
    ):
        binding = [mode, str(root), str(root)]
        assert any(wrapped[index : index + 3] == binding for index in range(len(wrapped) - 2))
    assert (venv_root / "pyvenv.cfg").is_file()
    assert (venv_root / "bin").is_dir()
    assert (venv_root / "lib").is_dir()

    result = process.run_cli(
        command,
        cwd=workspace_root,
        input_text="",
        timeout_s=10,
        env={
            "PATH": os.environ["PATH"],
            "CODEX_HOME": str(workspace_root),
        },
        sandbox_wrapper=wrapper,
    )

    assert result.timed_out is False
    assert result.returncode == 0
    assert set(json.loads(result.stdout)) == {"authenticated"}
