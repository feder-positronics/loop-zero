from pathlib import Path

import pytest

from conftest import REVISION, git, minimal_workflow
from loopzero import config


def test_minimal_profile_loads(consumer: Path):
    profile = config.load_profile(consumer)
    assert profile.core_revision == REVISION
    assert profile.checks == {"required": ("tests",), "advisory": (), "scheduled": ("health",)}
    assert profile.hooks == {"worktree_setup": ("make setup",), "acceptance": ("make test",)}
    assert profile.env_prefix == "LOOPZERO"
    assert profile.state_root_explicit is False
    assert profile.snapshot_version() == (Path(__file__).resolve().parents[2] / "core/VERSION").read_text().strip()


def test_profile_records_explicit_state_root(tmp_path: Path):
    (tmp_path / "workflow.toml").write_text(
        minimal_workflow(
            extra='[package]\nenv_prefix = "INTELFLO"\nstate_root = "/var/lib/intelflo"\n'
        ),
        encoding="utf-8",
    )
    profile = config.load_profile(tmp_path)
    assert profile.state_root == "/var/lib/intelflo"
    assert profile.state_root_explicit is True


def test_every_problem_is_reported_at_once(tmp_path: Path):
    (tmp_path / "workflow.toml").write_text(
        'profiles = ["ruby"]\n[core]\nrevision = "abc"\npath = "/abs"\n'
        '[checks]\nrequired = ["a"]\nadvisory = ["a"]\n'
        '[package]\nenv_prefix = "bad-prefix"\ncontract = "intelflo-v1"\nepoch = 0\n'
        '[hooks]\nunknown = "x"\nacceptance = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError) as info:
        config.load_profile(tmp_path)
    text = "\n".join(info.value.problems)
    for expected in (
        "profiles",
        "[core].repository",
        "[core].revision",
        "[core].path",
        "[checks].scheduled",
        "appears in more than one group",
        "env_prefix",
        "contract",
        "epoch",
        "[hooks].unknown",
        "[hooks].acceptance: empty",
    ):
        assert expected in text, expected


def test_routing_tiers_must_reference_aliases(tmp_path: Path):
    (tmp_path / "workflow.toml").write_text(
        minimal_workflow(
            extra='[routing.aliases]\nopus = { runner = "claude", model = "claude-opus-5" }\n'
            '[routing.tiers]\nS = { alias = "opus", effort = "high", read_only = true }\n'
            'B = { alias = "missing" }\n'
        ),
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError) as info:
        config.load_profile(tmp_path)
    assert "[routing.tiers].B" in str(info.value)


def test_missing_file_is_a_config_error(tmp_path: Path):
    with pytest.raises(config.ConfigError):
        config.load_profile(tmp_path)


def test_privileged_hooks_come_from_base_not_candidate(consumer: Path):
    git(consumer, "checkout", "-qb", "candidate")
    (consumer / "workflow.toml").write_text(
        minimal_workflow().replace('acceptance = ["make test"]', 'acceptance = ["true"]')
        + 'closeout = ["make closeout"]\n',
        encoding="utf-8",
    )
    profile = config.load_profile(consumer)
    assert profile.hooks["acceptance"] == ("true",)
    hooks = config.effective_hooks(profile, base_ref="main")
    assert hooks["acceptance"] == ("make test",), "candidate must not change its own acceptance"
    assert "closeout" not in hooks, "privileged hook absent at base is absent, not adopted"
    assert hooks["worktree_setup"] == ("make setup",)


def test_unavailable_base_is_a_blocker_not_no_hooks(consumer: Path):
    profile = config.load_profile(consumer)
    with pytest.raises(config.ConfigError) as info:
        config.effective_hooks(profile, base_ref="origin/does-not-exist")
    assert "unavailable" in str(info.value)
