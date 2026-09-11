import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.delivery import closeout


def _profile(root, **toolchain):
    values = {
        "closeout_snapshot_path": "trusted-tools",
        "closeout_temp_prefix": "consumer-closeout-",
        "trusted_bin_dir": "/usr/bin",
        **toolchain,
    }
    return SimpleNamespace(root=root, env_prefix="CONSUMER", toolchain=values)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


def test_settings_are_entirely_profile_owned(tmp_path):
    settings = closeout.CloseoutSettings.from_profile(
        _profile(
            tmp_path, closeout_snapshot_path="vendor/core",
            closeout_temp_prefix="my-closeout-", trusted_bin_dir="/bin",
        )
    )
    assert settings.snapshot_path == Path("vendor/core")
    assert settings.temp_prefix == "my-closeout-"
    assert settings.trusted_bin_dir == Path("/bin")
    assert settings.env("BOOTSTRAP") == "CONSUMER_BOOTSTRAP"


def test_isolated_environment_uses_configured_bin_and_prefix(tmp_path):
    env = closeout.isolated_environment(
        _profile(tmp_path), {"GH_TOKEN": "secret", "LD_PRELOAD": "bad", "IGNORED": "x"},
        home=tmp_path, user="owner",
    )
    assert env["PATH"] == "/usr/bin"
    assert env["GH_TOKEN"] == "secret"
    assert "LD_PRELOAD" not in env and "IGNORED" not in env


def test_materialize_and_cleanup_configured_snapshot(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "trusted-tools").mkdir()
    (repo / "trusted-tools" / "entry.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "snapshot")
    revision = _git(repo, "rev-parse", "HEAD")
    snapshot = closeout.materialize_snapshot(_profile(repo), revision=revision)
    try:
        assert snapshot.toolchain.joinpath("entry.py").read_text() == "pass\n"
        assert snapshot.checkout.name.startswith("consumer-closeout-")
    finally:
        closeout.cleanup_snapshot(_profile(repo), snapshot)
    assert not snapshot.checkout.exists()


def test_materialize_rejects_invalid_revision(tmp_path):
    with pytest.raises(closeout.BootstrapError, match="40 lowercase"):
        closeout.materialize_snapshot(_profile(tmp_path), revision="HEAD")
