from pathlib import Path
from types import SimpleNamespace

from loopzero.delivery import closeout


def _profile(root, **toolchain):
    values = {
        "closeout_snapshot_path": "trusted-tools",
        "closeout_temp_prefix": "consumer-closeout-",
        "trusted_bin_dir": "/usr/bin",
        **toolchain,
    }
    return SimpleNamespace(root=root, env_prefix="CONSUMER", toolchain=values)


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
    assert settings.launcher_path == Path("scripts/util/pr_closeout.py")
    assert settings.trust_floor_path == Path(
        "scripts/util/pr_closeout_trust_floor.json"
    )
    assert settings.env("BOOTSTRAP") == "CONSUMER_BOOTSTRAP"


def test_isolated_environment_uses_configured_bin_and_prefix(tmp_path):
    env = closeout.isolated_environment(
        _profile(tmp_path), {"GH_TOKEN": "secret", "LD_PRELOAD": "bad", "IGNORED": "x"},
        home=tmp_path, user="owner",
    )
    assert env["PATH"] == "/usr/bin"
    assert env["GH_TOKEN"] == "secret"
    assert "LD_PRELOAD" not in env and "IGNORED" not in env
def test_no_public_snapshot_materializer_bypasses_trusted_bootstrap():
    assert not hasattr(closeout, "materialize_snapshot")
    assert not hasattr(closeout, "cleanup_snapshot")


def test_main_passes_profile_configured_launcher_and_trust_floor(
    monkeypatch, tmp_path
):
    launcher = tmp_path / "trusted" / "closeout.py"
    captured = {}
    profile = _profile(
        tmp_path,
        closeout_launcher_path="trusted/closeout.py",
        closeout_trust_floor_path="trusted/floor.json",
    )

    def bootstrap(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return 17

    monkeypatch.setattr(closeout.sys, "flags", SimpleNamespace(isolated=True))
    monkeypatch.setattr(closeout, "_bootstrap", bootstrap)

    assert closeout.main(["--pr", "42"], profile=profile, launcher_path=launcher) == 17
    assert captured == {
        "argv": ["--pr", "42"],
        "launcher_path": launcher,
        "expected_launcher_path": Path("trusted/closeout.py"),
        "trust_floor_path": Path("trusted/floor.json"),
        "temp_prefix": "consumer-closeout-",
    }
