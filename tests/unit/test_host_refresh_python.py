"""Exercise the real host refresh command with isolated, synthetic vendor state."""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from loopzero import credential_seal
from loopzero.runners.process import LaunchSpec
from loopzero.runners.settings import get_settings


def test_seal_keeps_venv_interpreter_for_refresh(tmp_path, monkeypatch):
    environment = tmp_path / "venv"
    (environment / "bin").mkdir(parents=True)
    interpreter = environment / "bin/python"
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(credential_seal.sys, "executable", str(interpreter))
    tmp_path.chmod(0o700)

    @contextmanager
    def broker(**kwargs):
        assert get_settings().bridge_command(environment)[0] == str(interpreter)
        raise RuntimeError("stop after checking the refresh command")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="stop after checking"):
        credential_seal.seal_credential(
            "codex", output=tmp_path / "snapshot.json", broker=broker,
            sandbox_wrapper=lambda spec: spec.argv,
        )


def test_refresh_mounts_only_exact_python_installations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    venv = home / "venv"
    base = home / "managed/python"
    extensions = home / "managed/extensions"
    for path in (venv, base, extensions):
        path.mkdir(parents=True)
    monkeypatch.setattr(credential_seal.sys, "prefix", str(venv))
    monkeypatch.setattr(credential_seal.sys, "base_prefix", str(base))
    monkeypatch.setattr(credential_seal.sys, "base_exec_prefix", str(extensions))
    monkeypatch.delenv("LOOPZERO_LIVE_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(credential_seal.shutil, "which", lambda _: "/usr/bin/true")
    argv = credential_seal.host_refresh_wrapper()(LaunchSpec(
        argv=(str(venv / "bin/python"),), cwd=tmp_path,
        private_mounts=(), private_tmpdir=tmp_path / "private",
    ))
    readonly = [Path(argv[i + 1]) for i, arg in enumerate(argv) if arg == "--ro-bind"]
    assert {venv, base, extensions} <= set(readonly)
    assert home not in readonly
    assert home / "managed" not in readonly
    assert all(argv[i + 1] == argv[i + 2] for i, arg in enumerate(argv) if arg == "--ro-bind")


def _login(expiry: int, refresh: str) -> dict:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return {
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": f"fixture.{claims}.signature",
                   "id_token": "fixture-identity", "refresh_token": refresh,
                   "account_id": "fixture-account"},
        "last_refresh": "2026-09-15T00:00:00Z",
    }


@pytest.mark.parametrize("copies", [False, True], ids=["symlinks", "copies"])
@pytest.mark.parametrize("runtime", ["system", "managed"])
@pytest.mark.parametrize("host_sandbox", [False, True], ids=["bridge", "host-sandbox"])
def test_actual_host_refresh_preserves_python_environment(tmp_path, runtime, copies, host_sandbox):
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is required for the real refresh probe")
    if host_sandbox:
        readiness = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/usr", "/usr",
             "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
             "--symlink", "usr/lib64", "/lib64", "--", "/usr/bin/true"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        if readiness.returncode:
            pytest.skip(f"nested user namespaces unavailable: {readiness.stderr.strip()}")
    if runtime == "system":
        python = Path("/usr/bin/python3")
    else:
        configured = os.environ.get("LOOPZERO_TEST_MANAGED_PYTHON")
        python = Path(configured or sys._base_executable).resolve()
        if python.is_relative_to("/usr"):
            pytest.skip("set LOOPZERO_TEST_MANAGED_PYTHON to probe a managed installation")

    environment = tmp_path / "trusted-venv"
    subprocess.run(
        [str(python), "-I", "-m", "venv", "--without-pip",
         "--copies" if copies else "--symlinks", str(environment)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    interpreter = environment / "bin/python"
    site = Path(subprocess.check_output(
        [str(interpreter), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True, timeout=10,
    ).strip())
    source_package = Path(__file__).resolve().parents[2] / "src/loopzero"
    shutil.copytree(source_package, site / "loopzero", ignore=shutil.ignore_patterns("__pycache__"))
    source = tmp_path / "auth.json"
    output = tmp_path / "snapshot.json"
    original = _login(int(time.time()) + 3600, "fixture-original-host-refresh")
    rotated = _login(int(time.time()) + 10 * 86400, "fixture-rotated-host-refresh")
    source.write_text(json.dumps(original))
    source.chmod(0o600)
    tmp_path.chmod(0o700)

    # The vendor exists only in this venv. Its account method is reached only
    # through the production broker, bwrap wrapper and isolated-mode bridge.
    (site / "openai_codex.py").write_text(f"""
import errno
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

def CodexConfig(**kwargs):
    return kwargs

class Codex:
    def __init__(self, config):
        self.config = config
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def account(self, *, refresh_token):
        import ssl  # Also exercise the base runtime's extension modules.
        assert refresh_token is True
        assert sys.prefix == {str(environment)!r}
        if {host_sandbox!r}:
            assert not Path({str(source)!r}).exists()
        for path in (Path(sys.executable), Path(__file__), Path(ssl.__file__)) if {host_sandbox!r} else ():
            assert os.statvfs(path).f_flag & os.ST_RDONLY
        for path in (Path(__file__),) if {host_sandbox!r} else ():
            try:
                with path.open('ab'):
                    pass
            except OSError as exc:
                assert exc.errno == errno.EROFS
            else:
                raise AssertionError('runtime is writable')
        auth = Path(self.config['env']['CODEX_HOME']) / 'auth.json'
        assert json.loads(auth.read_bytes()) == {original!r}
        auth.write_text(json.dumps({rotated!r}))
        return SimpleNamespace(account=object())
""")
    entry = ["-m", "loopzero.cli"]
    if not host_sandbox:
        # Command/import coverage remains available when an outer validation
        # sandbox cannot nest user namespaces. This does not certify mounts.
        entry = ["-c", ("from loopzero import credential_seal, cli; "
                 "credential_seal._host_refresh_wrapper = lambda: lambda spec: spec.argv; "
                 "raise SystemExit(cli.main())")]
    result = subprocess.run(
        [str(interpreter), "-I", *entry, "accounts", "renew", "ci",
         "--source", str(source), "--out", str(output)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        check=False, capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(source.read_bytes()) == rotated
    sealed = json.loads(output.read_bytes())
    assert sealed["tokens"]["refresh_token"] == sealed["tokens"]["access_token"]
    assert b"fixture-original-host-refresh" not in output.read_bytes()
    assert b"fixture-rotated-host-refresh" not in output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert "fixture" not in result.stdout + result.stderr
