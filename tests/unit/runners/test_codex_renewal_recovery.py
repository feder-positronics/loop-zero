"""Recovery probes use synthetic native records and actual process boundaries."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loopzero.runners import codex

from .test_codex_credential_broker import _credential, _write_credential


def test_proven_popen_noexec_does_not_poison_native_renewal(tmp_path):
    path = tmp_path / "auth.json"
    original = _credential(expires_at_s=1_100)
    _write_credential(path, original)
    with pytest.raises(codex.CodexCredentialRefreshFailed):
        with codex.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            refresh_command=(sys.executable, "-I", "-c", "raise AssertionError"),
            sandbox_wrapper=lambda spec: [str(tmp_path / "absent-wrapper")],
        ):
            pytest.fail("failed exec exported a snapshot")
    assert json.loads(path.read_text()) == original
    assert not path.with_name(".auth.json.refresh-pending").exists()


@pytest.mark.parametrize("failure", ["injected-oserror", "started-exit"])
def test_unproven_failure_never_clears_native_pending(tmp_path, failure):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    def injected(*args, **kwargs):
        raise FileNotFoundError("synthetic caller assertion of no exec")
    options = {"run_refresh": injected} if failure == "injected-oserror" else {
        "sandbox_wrapper": lambda spec: spec.argv,
    }
    with pytest.raises(codex.CodexCredentialRefreshFailed):
        with codex.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            refresh_command=(sys.executable, "-I", "-c", "raise SystemExit(127)"),
            **options,
        ):
            pytest.fail("uncertain process exported a snapshot")
    pending = path.with_name(".auth.json.refresh-pending")
    before = pending.read_bytes()
    with pytest.raises(codex.CodexCredentialRefreshFailed):
        with codex.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=lambda *a, **k: pytest.fail("uncertain token was retried"),
            refresh_command=("unused",),
        ):
            pytest.fail("uncertain token admitted")
    assert pending.read_bytes() == before


def test_killed_after_same_token_install_recovers_under_stable_lock(tmp_path):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    source_root = Path(__file__).resolve().parents[3] / "src"
    probe = r'''
import base64,json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from loopzero.runners import codex
from loopzero.runners.settings import RuntimeSettings
root,mode,label=Path(sys.argv[2]),sys.argv[3],sys.argv[4]
settings=RuntimeSettings(env_prefix="RECOVERY_TEST",state_root=str(root/"state"))
expiry=1200 if mode=="owner" else 3000
claims=base64.urlsafe_b64encode(json.dumps({"exp":expiry}).encode()).decode().rstrip("=")
payload={"auth_mode":"chatgpt","OPENAI_API_KEY":None,"tokens":{"access_token":"h."+claims+".s","id_token":"opaque","refresh_token":"refresh","account_id":"account-1"},"last_refresh":"synthetic-success"}
program="import json,os; from pathlib import Path; assert os.pread(int(os.environ['RECOVERY_TEST_CODEX_AUTH_FD']),1048576,0); p=Path(os.environ['RECOVERY_TEST_CODEX_REFRESH_OUTPUT']); p.write_text("+repr(json.dumps(payload))+"); p.chmod(0o600); Path("+repr(str(root/('contact-'+label)))+").write_text('contact'); print('{\"authenticated\":true}')"
original_unlink=Path.unlink
pending=root/".auth.json.refresh-pending"
def pause_after_install(self,*args,**kwargs):
    if mode=="owner" and self==pending:
        (root/"durable-install").write_text("ready")
        while True: time.sleep(1)
    return original_unlink(self,*args,**kwargs)
Path.unlink=pause_after_install
with settings.use():
    (root/(label+"-started")).write_text("started")
    with codex.codex_subscription_credential(credential_path=root/"auth.json",requested_runtime_s=600,clock=lambda:1000,refresh_command=(sys.executable,"-I","-c",program),sandbox_wrapper=lambda spec:spec.argv) as fd:
        (root/(label+"-snapshot")).write_bytes(os.pread(fd,1048576,0))
'''
    def spawn(mode, label):
        return subprocess.Popen(
            [sys.executable, "-I", "-c", probe, str(source_root), str(tmp_path), mode, label],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    def wait_for(path):
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline, f"process never reached {path.name}"
            time.sleep(0.01)
    children = []
    try:
        owner = spawn("owner", "owner")
        children.append(owner)
        wait_for(tmp_path / "durable-install")
        lock = tmp_path / ".recovery_test_codex_refresh.lock"
        inode = lock.stat().st_ino
        for label in ("first", "second"):
            children.append(spawn("waiter", label))
            wait_for(tmp_path / f"{label}-started")
            assert not (tmp_path / f"{label}-snapshot").exists()
        os.killpg(owner.pid, signal.SIGKILL)
        owner.communicate(timeout=5)
        for child in children[1:]:
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, (stdout, stderr)
        assert lock.stat().st_ino == inode
        assert sum((tmp_path / f"contact-{label}").exists() for label in ("first", "second")) == 1
        for label in ("first", "second"):
            tokens = json.loads((tmp_path / f"{label}-snapshot").read_text())["tokens"]
            assert tokens["refresh_token"] == tokens["access_token"]
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)


@pytest.mark.parametrize("unsafe", ["exposed", "hardlinked", "unknown-key", "replaced"])
def test_recovery_key_never_adopts_unsafe_or_replaced_stable_lock(tmp_path, monkeypatch, unsafe):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    lock = tmp_path / codex.get_settings().lock_name("codex-refresh")
    lock.write_bytes(b"not-a-package-key" if unsafe == "unknown-key" else b"")
    lock.chmod(0o644 if unsafe == "exposed" else 0o600)
    if unsafe == "hardlinked":
        os.link(lock, tmp_path / "lock-alias")
    if unsafe == "replaced":
        flock = codex.fcntl.flock
        def replace_after_lock(fd, operation):
            flock(fd, operation)
            if operation == codex.fcntl.LOCK_EX:
                lock.rename(tmp_path / "old-lock")
                lock.write_bytes(b"")
                lock.chmod(0o600)
        monkeypatch.setattr(codex.fcntl, "flock", replace_after_lock)
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe lock admitted renewal authority")
    with pytest.raises(codex.UnsafeCodexCredential):
        with codex.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            run_refresh=forbidden, refresh_command=("unused",),
        ):
            pytest.fail("unsafe lock exported snapshot")


@pytest.mark.parametrize("route", ["wrapper", "refresh"])
def test_injected_broker_noexec_exception_cannot_clear_intent(tmp_path, route):
    path = tmp_path / "auth.json"
    _write_credential(path, _credential(expires_at_s=1_100))
    def impersonate(*args, **kwargs):
        raise codex._CodexRefreshNotStarted("caller invented evidence")
    options = {"sandbox_wrapper": impersonate} if route == "wrapper" else {"run_refresh": impersonate}
    with pytest.raises(codex.CodexCredentialRefreshFailed):
        with codex.codex_subscription_credential(
            credential_path=path, requested_runtime_s=600, clock=lambda: 1_000,
            refresh_command=(sys.executable, "-I", "-c", "raise SystemExit(127)"), **options,
        ):
            pytest.fail("injected evidence admitted snapshot")
    assert path.with_name(".auth.json.refresh-pending").exists()
