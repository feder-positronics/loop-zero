"""Focused tests for the trusted host-side Codex credential broker."""

import base64
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_broker():
    from loopzero.runners import codex as codex_credential

    return codex_credential


codex_credential = _load_broker()


def _test_bwrap_argv(
    spec,
    *,
    tooling_root: Path,
    venv_root: Path,
    bridge_dir: Path,
    workspace_root: Path,
    state_root: Path,
) -> list[str]:
    """Small test-only wrapper demonstrating the kernel/runner argv seam."""
    argv = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        # Python needs /dev/urandom for hash randomization and /proc for its
        # own startup; a kernel wrapper must provide both to any child.
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        # Hide the consumer state tree from the broad test-only root bind.
        "--tmpfs",
        str(state_root),
    ]
    for root in (tooling_root, venv_root, bridge_dir):
        argv.extend(("--ro-bind", str(root), str(root)))
    argv.extend(("--bind", str(workspace_root), str(workspace_root)))
    for root in (*spec.private_mounts, spec.private_tmpdir):
        argv.extend(("--dir", str(root)))
        argv.extend(("--bind", str(root), str(root)))
    return [*argv, "--", *spec.argv]


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
    tokens = payload["tokens"]
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": tokens["access_token"],
            "id_token": tokens["id_token"],
            "refresh_token": tokens["access_token"],
            "account_id": tokens["account_id"],
        },
        "last_refresh": payload["last_refresh"],
    }


def test_codex_snapshot_drops_unrecognized_credential_fields() -> None:
    payload = _credential(expires_at_s=3_000)
    payload["future_secret"] = "must-not-reach-worker"
    payload["tokens"]["future_refresh"] = "must-not-reach-worker"
    validated = codex_credential._validate_payload(json.dumps(payload).encode())

    snapshot = codex_credential._sandbox_snapshot_payload(validated)

    assert b"must-not-reach-worker" not in snapshot
    assert json.loads(snapshot) == _runtime_snapshot(payload)


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


def test_expiring_access_only_codex_credential_is_cleanly_unavailable(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".codex" / "auth.json"
    payload = _credential(expires_at_s=1_100)
    payload["tokens"]["refresh_token"] = payload["tokens"]["access_token"]
    _write_credential(credential, payload)

    with pytest.raises(codex_credential.CodexCredentialUnavailable):
        with codex_credential.codex_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_refresh=lambda *_args, **_kwargs: pytest.fail(
                "access-only credentials must never refresh"
            ),
        ):
            pass


def test_near_expiry_codex_credential_refreshes_and_persists_rotated_token(
    tmp_path: Path,
) -> None:
    from loopzero.runners.settings import RuntimeSettings

    credential = tmp_path / ".codex" / "auth.json"
    tooling_root = tmp_path / "tooling"
    state_root = tmp_path / "state"
    settings = RuntimeSettings(tooling_root=tooling_root, state_root=str(state_root))
    workspace_root = settings.workspace_root(tooling_root)
    original = _credential(expires_at_s=1_100, refresh_token="old-refresh")
    refreshed = _credential(expires_at_s=3_000, refresh_token="new-refresh")
    _write_credential(credential, original)
    observed: dict[str, object] = {}

    def refresh(command, *, env, **kwargs):
        observed["command"] = command
        observed["timeout"] = kwargs["timeout"]
        staging = Path(env["CODEX_HOME"]) / "auth.json"
        assert staging.parent.parent == state_root
        assert not staging.parent.is_relative_to(workspace_root)
        assert stat.S_IMODE(staging.parent.stat().st_mode) == 0o700
        observed["staging_home"] = staging.parent
        assert not staging.exists()
        credential_fd = int(env[settings.env_name("CODEX_AUTH_FD")])
        assert kwargs["pass_fds"] == (credential_fd,)
        assert json.loads(os.pread(credential_fd, 1024 * 1024, 0)) == original
        _write_credential(staging, refreshed)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"authenticated":true}',
            stderr="",
        )

    with settings.use():
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
            (credential.parent / settings.lock_name("codex-refresh")).stat().st_mode
        )
        == 0o600
    )
    assert observed["command"] == [
        "trusted-python",
        "trusted-bridge",
        "trusted-codex",
    ]
    assert observed["timeout"] == codex_credential.REFRESH_TIMEOUT_S
    assert not Path(observed["staging_home"]).exists()


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


def test_codex_refresh_persists_short_expiry_without_export(
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

    with pytest.raises(codex_credential.CodexCredentialUnavailable):
        with codex_credential.codex_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_refresh=refresh,
            refresh_command=("trusted-python", "trusted-bridge", "trusted-codex"),
        ):
            pass

    assert json.loads(credential.read_text(encoding="utf-8")) == _credential(expires_at_s=1_200)


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
    wrapper = lambda spec: ["bwrap", "--", *spec.argv]

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
        "pass_fds": (),
        "private_mounts": (),
        "sandbox_wrapper": wrapper,
    }


def test_default_refresh_runner_owns_the_credential_descriptor_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    credential_path = tmp_path / ".codex" / "auth.json"
    original = _credential(expires_at_s=1_100)
    refreshed_payload = _credential(expires_at_s=3_000)
    _write_credential(credential_path, original)
    credential = codex_credential._read_credential(credential_path)
    real_close = os.close
    close_callers: list[str] = []

    def tracked_close(descriptor: int) -> None:
        close_callers.append(sys._getframe(1).f_code.co_name)
        real_close(descriptor)

    def contained_run(command, *, env, cwd, **_kwargs):
        descriptor = int(env["INTELFLO_CODEX_AUTH_FD"])
        tracked_close(descriptor)
        _write_credential(Path(env["CODEX_HOME"]) / "auth.json", refreshed_payload)
        return codex_credential.ProcessResult(
            returncode=0,
            stdout='{"authenticated":true}',
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    monkeypatch.setattr(codex_credential, "default_run_cli", contained_run)
    monkeypatch.setattr(codex_credential.os, "close", tracked_close)

    refreshed = codex_credential._refresh_credential(
        credential,
        run_refresh=codex_credential._run_refresh_process_group,
        refresh_command=("trusted-python", "trusted-bridge", "--codex-refresh"),
        sandbox_wrapper=lambda spec: spec.argv,
    )

    assert refreshed.payload == json.dumps(refreshed_payload).encode("utf-8")
    assert "contained_run" in close_callers
    assert "_refresh_credential" not in close_callers


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


def test_refresh_bridge_consumes_sealed_input_and_writes_only_rotated_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from loopzero.runners import bridge
    from loopzero.runners.settings import get_settings

    original = _credential(expires_at_s=1_100, refresh_token="durable-original")
    refreshed = _credential(expires_at_s=3_000, refresh_token="durable-rotated")
    staging_home = tmp_path / "staging"
    private_tmpdir = tmp_path / "private-tmp"
    staging_home.mkdir(mode=0o700)
    private_tmpdir.mkdir(mode=0o700)
    output = staging_home / "auth.json"
    observed: dict[str, object] = {}

    class FakeCodex:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def account(self, *, refresh_token):
            auth = Path(self.config["env"]["CODEX_HOME"]) / "auth.json"
            observed["auth_home"] = auth.parent
            observed["input"] = json.loads(auth.read_text(encoding="utf-8"))
            assert not output.exists()
            _write_credential(auth, refreshed)
            return SimpleNamespace(account=object())

    fake_sdk = ModuleType("openai_codex")
    fake_sdk.Codex = FakeCodex
    fake_sdk.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
    monkeypatch.setattr(bridge.sys, "argv", ["bridge.py", "--codex-refresh"])
    descriptor = codex_credential._snapshot_descriptor(
        json.dumps(original).encode("utf-8")
    )
    settings = get_settings()
    monkeypatch.setenv(settings.env_name("CODEX_AUTH_FD"), str(descriptor))
    monkeypatch.setenv(settings.env_name("CODEX_REFRESH_OUTPUT"), str(output))
    monkeypatch.setenv("CODEX_HOME", str(staging_home))
    monkeypatch.setenv("TMPDIR", str(private_tmpdir))
    monkeypatch.setattr(tempfile, "tempdir", None)

    assert bridge.codex_refresh() == 0
    assert observed["input"] == original
    assert Path(observed["auth_home"]).is_relative_to(private_tmpdir)
    assert not Path(observed["auth_home"]).exists()
    assert json.loads(output.read_text(encoding="utf-8")) == refreshed
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(capsys.readouterr().out) == {"authenticated": True}


def test_codex_refresh_wrapper_binds_sdk_venv_bridge_and_workspace(
    tmp_path: Path,
    runtime_settings,
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
        output = probe.stderr or probe.stdout
        diagnostic = output.strip().splitlines()
        detail = diagnostic[-1] if diagnostic else "unknown namespace error"
        namespace_denied = (
            "No permissions to create a new namespace",
            "Creating new namespace failed: Operation not permitted",
        )
        if any(message in output for message in namespace_denied):
            pytest.skip(f"bubblewrap cannot create a user namespace: {detail}")
        pytest.fail(f"bubblewrap probe failed for a non-namespace reason: {detail}")

    from loopzero.runners import process

    tooling_root = Path(__file__).resolve().parents[3]
    venv_root = Path(sys.prefix).resolve()
    bridge = Path(codex_credential.__file__).resolve().with_name("bridge.py")
    bridge_dir = bridge.parent
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    state_root = Path(runtime_settings.state_root)
    state_root.mkdir(mode=0o700)
    command = [sys.executable, "-I", str(bridge), "--codex-refresh"]

    observed_specs = []
    observed_argv = []

    def wrapper(spec):
        observed_specs.append(spec)
        argv = _test_bwrap_argv(
            spec,
            tooling_root=tooling_root,
            venv_root=venv_root,
            bridge_dir=bridge_dir,
            workspace_root=workspace_root,
            state_root=state_root,
        )
        observed_argv.append(argv)
        return argv

    private_tmpdir = state_root / "private-tmp"
    private_tmpdir.mkdir()
    launch_spec = process.LaunchSpec(
        argv=tuple(command),
        cwd=workspace_root,
        private_mounts=(),
        private_tmpdir=private_tmpdir,
    )
    wrapped = wrapper(launch_spec)
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
    actual_spec = observed_specs[-1]
    actual_argv = observed_argv[-1]
    assert actual_spec.cwd == workspace_root
    assert actual_spec.private_tmpdir != private_tmpdir
    assert ["--bind", str(actual_spec.private_tmpdir), str(actual_spec.private_tmpdir)] in [
        actual_argv[index : index + 3]
        for index in range(len(actual_argv) - 2)
    ]
    assert not actual_spec.private_tmpdir.exists()


def test_codex_snapshot_carries_last_refresh_so_app_server_uses_the_access_token() -> None:
    payload = _credential(expires_at_s=3_000)
    validated = codex_credential._validate_payload(json.dumps(payload).encode())

    snapshot = json.loads(codex_credential._sandbox_snapshot_payload(validated))

    assert snapshot["last_refresh"] == "2026-08-31T00:00:00Z"
    assert snapshot["tokens"]["refresh_token"] == snapshot["tokens"]["access_token"]

    del payload["last_refresh"]
    validated = codex_credential._validate_payload(json.dumps(payload).encode())
    stamped = json.loads(codex_credential._sandbox_snapshot_payload(validated))
    assert isinstance(stamped["last_refresh"], str)
    assert stamped["last_refresh"].endswith("Z")
    assert set(stamped) == {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}


@pytest.mark.parametrize("replacement_expiry", [1_050, 1_100, 1_200])
def test_short_rotation_is_saved_before_snapshot_refusal(tmp_path, replacement_expiry):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100, refresh_token="old"))
    replacement = _credential(expires_at_s=replacement_expiry, refresh_token="rotated")

    def vendor(command, *, env, **kwargs):
        _write_credential(Path(env["CODEX_HOME"]) / "auth.json", replacement)
        return subprocess.CompletedProcess(command, 0, stdout='{"authenticated":true}')

    with pytest.raises(codex_credential.CodexCredentialError):
        with codex_credential.codex_subscription_credential(
            requested_runtime_s=600, credential_path=path, clock=lambda: 1_000,
            run_refresh=vendor, refresh_command=("fake-vendor",),
        ):
            pytest.fail("insufficient snapshot escaped")
    assert json.loads(path.read_text()) == replacement


@pytest.mark.parametrize("failure", ["timeout", "install", "account", "status"])
def test_uncertain_rotation_blocks_spent_token_until_new_host_login(tmp_path, monkeypatch, failure):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100, refresh_token="spent"))
    calls = []
    install = codex_credential._install_credential

    def vendor(command, *, env, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        replacement = _credential(expires_at_s=3_000, refresh_token="rotated")
        if failure == "account":
            replacement["tokens"]["account_id"] = "different-account"
        _write_credential(Path(env["CODEX_HOME"]) / "auth.json", replacement)
        return subprocess.CompletedProcess(command, 1 if failure == "status" else 0,
                                           stdout='{"authenticated":true}')

    def broken_install(*args):
        raise OSError("synthetic install failure")

    if failure == "install":
        monkeypatch.setattr(codex_credential, "_install_credential", broken_install)
    arguments = dict(requested_runtime_s=600, credential_path=path, clock=lambda: 1_000,
                     run_refresh=vendor, refresh_command=("fake-vendor",))
    for _ in range(2):
        with pytest.raises((codex_credential.CodexCredentialError, OSError)):
            with codex_credential.codex_subscription_credential(**arguments):
                pytest.fail("uncertain rotation escaped")
    assert len(calls) == 1
    # Editing access expiry cannot authorize a retry of the same spent refresh token.
    _write_credential(path, _credential(expires_at_s=1_200, refresh_token="spent"))
    with pytest.raises(codex_credential.CodexCredentialError):
        with codex_credential.codex_subscription_credential(**arguments):
            pytest.fail("spent token was retried")
    assert len(calls) == 1
    monkeypatch.setattr(codex_credential, "_install_credential", install)
    _write_credential(path, _credential(expires_at_s=3_000, refresh_token="new-login"))
    with codex_credential.codex_subscription_credential(**arguments):
        pass
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["symlink", "malformed", "public"])
def test_unsafe_pending_rotation_state_blocks_vendor(tmp_path, state):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    pending = tmp_path / ".auth.json.refresh-pending"
    if state == "symlink":
        pending.symlink_to(tmp_path / "absent")
    else:
        pending.write_bytes(b"broken" if state == "malformed" else b"0" * 64)
        pending.chmod(0o644 if state == "public" else 0o600)

    def vendor(*args, **kwargs):
        pytest.fail("unsafe recovery state allowed a vendor call")

    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            requested_runtime_s=600, credential_path=path, clock=lambda: 1_000,
            run_refresh=vendor, refresh_command=("fake-vendor",),
        ):
            pytest.fail("unsafe recovery state exported a snapshot")


def test_pending_rotation_fifo_fails_without_waiting_for_writer(tmp_path):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    os.mkfifo(tmp_path / ".auth.json.refresh-pending", 0o600)
    probe = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from loopzero.runners.codex import codex_subscription_credential, UnsafeCodexCredential
try:
    with codex_subscription_credential(credential_path=Path(sys.argv[1]),
            requested_runtime_s=600, clock=lambda: 1000,
            run_refresh=lambda *a, **k: None, refresh_command=("fake",)):
        raise AssertionError("unsafe snapshot")
except UnsafeCodexCredential:
    pass
'''
    source_root = Path(__file__).resolve().parents[3] / "src"
    subprocess.run(
        [sys.executable, "-I", "-c", probe, str(path), str(source_root)],
        timeout=3,
        check=True,
    )


def test_pending_rotation_hardlink_is_rejected(tmp_path):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    pending = tmp_path / ".auth.json.refresh-pending"
    pending.write_bytes(b"0" * 64)
    pending.chmod(0o600)
    os.link(pending, tmp_path / "alias")
    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=lambda *a, **k: pytest.fail("hardlink permitted vendor call"),
            refresh_command=("fake",),
        ):
            pytest.fail("unsafe snapshot")


@pytest.mark.parametrize("failure", ["create", "unlink", "fsync"])
def test_pending_rotation_io_failures_are_content_free(tmp_path, monkeypatch, failure):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    pending = tmp_path / ".auth.json.refresh-pending"

    def fail(*args, **kwargs):
        raise OSError("private-path-and-token-material")

    if failure == "create":
        monkeypatch.setattr(codex_credential, "_write_private_file", fail)
    elif failure == "fsync":
        monkeypatch.setattr(codex_credential, "_sync_credential_directory", fail)
    else:
        pending.write_bytes(b"0" * 64)
        pending.chmod(0o600)
        unlink = Path.unlink

        def checked_unlink(self, *args, **kwargs):
            if self == pending:
                fail()
            return unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", checked_unlink)
    with pytest.raises(codex_credential.CodexCredentialError) as error:
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=lambda *a, **k: pytest.fail("failed intent permitted vendor call"),
            refresh_command=("fake",),
        ):
            pytest.fail("unsafe snapshot")
    assert "private-path-and-token-material" not in str(error.value)


@pytest.mark.parametrize("unsafe", ["symlink", "world-writable"])
def test_unsafe_host_directory_prevents_vendor_rotation(tmp_path, unsafe):
    parent = tmp_path / "host"
    path = parent / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    if unsafe == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        path = alias / "auth.json"
    else:
        parent.chmod(0o777)
    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=lambda *a, **k: pytest.fail("unsafe directory permitted vendor call"),
            refresh_command=("fake",),
        ):
            pytest.fail("unsafe snapshot")
    assert not (parent / ".auth.json.refresh-pending").exists()


def _account_jwt(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"header.{encoded.decode()}.signature"


@pytest.mark.parametrize("token_name", ["access_token", "id_token"])
@pytest.mark.parametrize("auth", [
    {"chatgpt_account_id": "account-2"},
    {"chatgpt_account_id": ""},
    {"chatgpt_account_id": 12},
    {"chatgpt_account_id": True},
    {"chatgpt_account_id": []},
    {"chatgpt_account_id": {}},
    "invalid-namespace", [], False,
])
def test_codex_account_claim_inconsistency_rejects_native_source(tmp_path, token_name, auth):
    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000)
    payload["tokens"][token_name] = _account_jwt({
        "exp": 3_000, "https://api.openai.com/auth": auth,
    })
    _write_credential(path, payload)
    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=lambda *a, **k: pytest.fail("inconsistent source reached vendor"),
        ):
            pytest.fail("inconsistent source exported")


@pytest.mark.parametrize("identity", [
    "header.@@@.signature", "header.bm90LWpzb24.signature",
    _account_jwt([]), _account_jwt(1), "header..signature", "header.e30.",
])
def test_codex_jwt_shaped_identity_must_decode_an_object(tmp_path, identity):
    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000)
    payload["tokens"]["id_token"] = identity
    _write_credential(path, payload)
    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
        ):
            pytest.fail("invalid identity JWT exported")


@pytest.mark.parametrize("token_name", ["access_token", "id_token"])
def test_codex_account_claim_inconsistency_after_rotation_keeps_recovery_marker(tmp_path, token_name):
    path = tmp_path / "auth.json"
    original = _credential(expires_at_s=1_100, refresh_token="original-refresh")
    original["tokens"]["access_token"] = _account_jwt({
        "exp": 1_100, "https://api.openai.com/auth": {"chatgpt_account_id": "account-1"},
    })
    _write_credential(path, original)
    replacement = _credential(expires_at_s=3_000, refresh_token="rotated-refresh")
    replacement["tokens"][token_name] = _account_jwt({
        "exp": 3_000, "https://api.openai.com/auth": {"chatgpt_account_id": "account-2"},
    })
    calls = []

    def vendor(command, *, env, **kwargs):
        calls.append(command)
        _write_credential(Path(env["CODEX_HOME"]) / "auth.json", replacement)
        return subprocess.CompletedProcess(command, 0, stdout='{"authenticated":true}')

    arguments = dict(credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
                     run_refresh=vendor, refresh_command=("fake-vendor",))
    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(**arguments):
            pytest.fail("inconsistent replacement exported")
    assert json.loads(path.read_text()) == original
    assert (tmp_path / ".auth.json.refresh-pending").is_file()
    with pytest.raises(codex_credential.CodexCredentialRefreshFailed):
        with codex_credential.codex_subscription_credential(**arguments):
            pytest.fail("spent refresh token retried")
    assert len(calls) == 1


@pytest.mark.parametrize("auth", [None, {}, {"chatgpt_account_id": None},
                                  {"chatgpt_account_id": "account-1"}])
@pytest.mark.parametrize("identity", [None, "opaque", "opaque.with-dot", "a.b.c.d"])
def test_codex_account_claim_compatibility_is_not_identity_proof(tmp_path, auth, identity):
    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000)
    payload["tokens"]["access_token"] = _account_jwt({
        "exp": 3_000, "sub": "different-access-subject", "chatgpt_account_id": "ignored",
        "https://api.openai.com/auth": auth,
    })
    payload["tokens"]["id_token"] = identity or _account_jwt({
        "sub": "different-id-subject", "email": "synthetic@example.invalid",
        "https://api.openai.com/auth": {
            **(auth or {}), "chatgpt_user_id": "different-user", "user_id": "other-user",
        },
    })
    _write_credential(path, payload)
    with codex_credential.codex_subscription_credential(
        credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
        run_refresh=lambda *a, **k: pytest.fail("fresh compatible source refreshed"),
    ) as descriptor:
        assert json.loads(os.pread(descriptor, 1024 * 1024, 0)) == _runtime_snapshot(payload)


def _matching_pending(path, payload):
    pending = path.with_name(f".{path.name}.refresh-pending")
    validated = codex_credential._validate_payload(json.dumps(payload).encode())
    pending.write_bytes(codex_credential._refresh_fingerprint(validated))
    pending.chmod(0o600)
    return pending


@pytest.mark.parametrize("runtime, admitted", [(600, True), (1_699, True), (1_700, False), (1_701, False)])
def test_pending_refresh_only_blocks_renewal_not_fresh_access(tmp_path, runtime, admitted):
    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000)
    _write_credential(path, payload)
    pending = _matching_pending(path, payload)
    before = (pending.read_bytes(), pending.stat().st_mode, pending.stat().st_ino)
    arguments = dict(credential_path=path, requested_runtime_s=runtime, clock=lambda: 1_000,
                     run_refresh=lambda *a, **k: pytest.fail("uncertain token reached vendor"))
    if admitted:
        with codex_credential.codex_subscription_credential(**arguments) as descriptor:
            assert json.loads(os.pread(descriptor, 1024 * 1024, 0)) == _runtime_snapshot(payload)
    else:
        with pytest.raises(codex_credential.CodexCredentialRefreshFailed):
            with codex_credential.codex_subscription_credential(**arguments):
                pytest.fail("insufficient snapshot exported")
    assert (pending.read_bytes(), pending.stat().st_mode, pending.stat().st_ino) == before


def test_pending_fresh_admission_rechecks_final_horizon(tmp_path):
    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000)
    _write_credential(path, payload)
    pending = _matching_pending(path, payload)
    before = pending.read_bytes()
    times = iter([1_000, 2_100])
    with pytest.raises(codex_credential.CodexCredentialUnavailable):
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: next(times),
            run_refresh=lambda *a, **k: pytest.fail("elapsed time authorized renewal"),
        ):
            pytest.fail("expired snapshot exported")
    assert pending.read_bytes() == before


@pytest.mark.parametrize("unsafe", ["malformed", "public", "hardlink", "symlink", "account"])
def test_pending_fresh_admission_still_validates_marker_and_account(tmp_path, unsafe):
    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000)
    _write_credential(path, payload)
    pending = _matching_pending(path, payload)
    if unsafe == "malformed":
        pending.write_bytes(b"invalid")
    elif unsafe == "public":
        pending.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(pending, tmp_path / "alias")
    elif unsafe == "symlink":
        pending.rename(tmp_path / "target")
        pending.symlink_to(tmp_path / "target")
    else:
        payload["tokens"]["id_token"] = _account_jwt({
            "https://api.openai.com/auth": {"chatgpt_account_id": "other-account"},
        })
        _write_credential(path, payload)
    with pytest.raises(codex_credential.UnsafeCodexCredential):
        with codex_credential.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=lambda *a, **k: pytest.fail("unsafe state reached vendor"),
        ):
            pytest.fail("unsafe fresh snapshot exported")


def test_interrupted_refresh_allows_concurrent_short_runs_without_retry(tmp_path):
    import signal
    import time

    path = tmp_path / "auth.json"
    payload = _credential(expires_at_s=3_000, refresh_token="possibly-spent")
    _write_credential(path, payload)
    source_root = Path(__file__).resolve().parents[3] / "src"
    probe = '''
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from loopzero.runners.codex import codex_subscription_credential, CodexCredentialRefreshFailed
from loopzero.runners.settings import RuntimeSettings
root, mode, label = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
settings = RuntimeSettings(state_root=str(root / "state"), env_prefix=sys.argv[5])
def vendor(command, *, env, pass_fds, **kwargs):
    if mode != "interrupt":
        (root / "unexpected-vendor").write_text("called")
        raise AssertionError("uncertain refresh retried")
    child = "import os,sys,time; from pathlib import Path; assert os.pread(int(sys.argv[1]),1048576,0); Path(sys.argv[2]).write_text('contact'); time.sleep(30)"
    return subprocess.run([sys.executable, "-I", "-c", child,
        str(pass_fds[0]), str(root / "vendor-contact")], pass_fds=pass_fds)
with settings.use():
    (root / (label + "-started")).write_text("started")
    try:
        with codex_subscription_credential(credential_path=root / "auth.json",
                requested_runtime_s=3_000 if mode in {"interrupt", "blocked"} else 600,
                clock=lambda: 1_000, run_refresh=vendor, refresh_command=("fake",)) as fd:
            if mode != "snapshot":
                raise AssertionError("insufficient snapshot exported")
            (root / label).write_bytes(os.pread(fd,1048576,0))
    except CodexCredentialRefreshFailed:
        if mode != "blocked":
            raise
        (root / label).write_text("blocked")
'''

    def spawn(mode, label):
        return subprocess.Popen(
            [sys.executable, "-I", "-c", probe, str(source_root), str(tmp_path), mode, label,
             codex_credential.get_settings().env_prefix],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def wait_for(path):
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline, f"synthetic process did not reach {path.name}"
            time.sleep(0.01)

    processes = []
    try:
        owner = spawn("interrupt", "owner")
        processes.append(owner)
        wait_for(tmp_path / "vendor-contact")
        pending = tmp_path / ".auth.json.refresh-pending"
        before = (pending.read_bytes(), pending.stat().st_mode, pending.stat().st_ino)
        lock = tmp_path / codex_credential.get_settings().lock_name("codex-refresh")
        lock_inode = lock.stat().st_ino
        for mode, label in [("snapshot", "short1"), ("snapshot", "short2"), ("blocked", "long")]:
            processes.append(spawn(mode, label))
            wait_for(tmp_path / (label + "-started"))
            assert not (tmp_path / label).exists()
        os.killpg(owner.pid, signal.SIGKILL)
        owner.communicate(timeout=5)
        for child in processes[1:]:
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, (stdout, stderr)
        for label in ("short1", "short2"):
            assert json.loads((tmp_path / label).read_text()) == _runtime_snapshot(payload)
        assert (tmp_path / "long").read_text() == "blocked"
        assert not (tmp_path / "unexpected-vendor").exists()
        assert (pending.read_bytes(), pending.stat().st_mode, pending.stat().st_ino) == before
        assert lock.stat().st_ino == lock_inode
        assert json.loads(path.read_text()) == payload
    finally:
        for child in processes:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
