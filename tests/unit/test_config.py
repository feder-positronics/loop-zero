from pathlib import Path

import pytest

from conftest import REVISION, git, minimal_workflow
from loopzero import config


def test_minimal_profile_loads(consumer: Path):
    profile = config.load_profile(consumer)
    assert profile.core_revision == REVISION
    assert profile.checks == {"required": ("tests",), "advisory": (), "scheduled": ("health",)}
    assert profile.hooks == {"worktree_setup": ("make setup",), "acceptance": ("make test",)}
    assert profile.env_prefix == "INTELFLO"
    assert profile.state_root_explicit is False
    assert profile.toolchain["dotenv"] == "fastapi_backend/.env"
    assert profile.toolchain["db_targets"][0] == "test"
    assert profile.state_root == "~/.local/state/intelflo"
    assert profile.routing_budgets == {"medium": 5.0, "high": 10.0}
    assert profile.compatible_policy_versions == (
        "2026-07-24-v9", "2026-08-06-v10", "2026-08-17-v11"
    )
    assert {"fable", "opus", "sol", "terra", "luna"} <= profile.aliases.keys()
    assert profile.toolchain["db_url_vars"] == ["TEST_DATABASE_URL", "DATABASE_URL"]
    assert profile.toolchain["db_lock"] == "/tmp/intelflo-testdb-5433.lock"
    assert profile.security_patterns
    assert profile.path_classes["backend-risk"]
    assert profile.github.labels["standalone"] == "standalone"
    assert profile.github.body_required_sections == ("Context and goal", "Validation")
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


def test_mechanism_configuration_is_typed_and_retained():
    profile = config.load_profile(
        Path(__file__).resolve().parents[1] / "example_consumer"
    )
    assert profile.toolchain["db_targets"] == ["test-integration"]
    assert profile.routing_budgets == {"medium": 5.0, "high": 10.0}
    assert profile.routing_policy_version == "2026-08-17-v11"
    assert profile.required_sections == ("code", "security")
    assert profile.github.labels["standalone"] == "standalone"
    assert profile.github.gh_version_floor == (2, 40, 0)


def test_mechanism_configuration_reports_all_invalid_keys(tmp_path: Path):
    (tmp_path / "workflow.toml").write_text(
        minimal_workflow(
            extra="""
[toolchain]
unknown = true
db_lock = "relative.lock"
db_url_vars = ["bad-name"]
[routing]
unknown = true
default_timeout_s = 0
[routing.budgets]
high = -1
[review]
unknown = true
required_sections = ["", "code", "code"]
finding_severities = ["critical", "unknown"]
[github]
unknown = true
gh_version_floor = "new"
retries = 11
body_required_sections = ["Summary", "Summary"]
[path_classes]
Bad = ["/absolute"]
"""
        ),
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError) as info:
        config.load_profile(tmp_path)
    text = str(info.value)
    for expected in (
        "[toolchain].unknown",
        "[toolchain].db_lock",
        "[toolchain].db_url_vars",
        "[routing].unknown",
        "[routing].default_timeout_s",
        "[routing.budgets].high",
        "[review].unknown",
        "required_sections",
        "finding_severities",
        "[github].unknown",
        "gh_version_floor",
        "[github].retries",
        "body_required_sections",
        "[path_classes].Bad",
    ):
        assert expected in text


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
    base = config.resolve_base(consumer, base_ref="refs/heads/main")
    hooks = config.effective_hooks(profile, base)
    assert hooks["acceptance"] == ("make test",), "candidate must not change its own acceptance"
    assert "closeout" not in hooks, "privileged hook absent at base is absent, not adopted"
    assert hooks["worktree_setup"] == ("make setup",)


def test_unavailable_base_is_a_blocker_not_no_hooks(consumer: Path):
    with pytest.raises(config.ConfigError) as info:
        config.resolve_base(consumer, base_ref="refs/remotes/origin/does-not-exist")
    assert "unavailable" in str(info.value)


@pytest.mark.parametrize("base_ref", ["HEAD", "HEAD~1", "main"])
def test_base_ref_rejects_revision_expressions_before_git(
    consumer: Path, monkeypatch, base_ref: str
):
    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("invalid base refs must be rejected before invoking git")

    monkeypatch.setattr(config, "_git", unexpected_git)
    with pytest.raises(config.ConfigError, match="full named ref"):
        config.resolve_base(consumer, base_ref=base_ref)


def test_packed_full_base_ref_is_accepted(consumer: Path):
    expected = git(consumer, "rev-parse", "refs/heads/main").strip()
    git(consumer, "pack-refs", "--all", "--prune")
    assert not (consumer / ".git/refs/heads/main").exists()
    assert config.resolve_base(consumer, base_ref="refs/heads/main") == expected


@pytest.mark.parametrize(
    ("workflow", "problem"),
    [
        (minimal_workflow() + "\n[routing]\naliases = []\n", "[routing.aliases]"),
        (minimal_workflow() + "\n[routing]\ntiers = \"wrong\"\n", "[routing.tiers]"),
        ("path_classes = []\n" + minimal_workflow(), "[path_classes]"),
        (
            minimal_workflow()
            + '\n[routing.aliases]\nbad = { runner = "fake", model = "fake", write = "false" }\n',
            ".write",
        ),
        (
            minimal_workflow()
            + '\n[routing.aliases]\nbad = { runner = "fake", model = "fake", agent = 1 }\n',
            ".agent",
        ),
        (
            minimal_workflow()
            + '\n[routing.aliases]\nok = { runner = "fake", model = "fake" }\n'
            + '[routing.tiers]\nC = { alias = "ok", read_only = "false" }\n',
            ".read_only",
        ),
        (
            minimal_workflow()
            + '\n[routing.aliases]\nok = { runner = "fake", model = "fake" }\n'
            + '[routing.tiers]\nC = { alias = "ok", effort = "ultra" }\n',
            ".effort",
        ),
        (
            "hooks = []\n"
            + minimal_workflow().replace(
                '[hooks]\nworktree_setup = ["make setup"]\nacceptance = ["make test"]\n',
                "",
            ),
            "[hooks]",
        ),
    ],
)
def test_wrong_nested_toml_types_are_config_errors(tmp_path: Path, workflow: str, problem: str):
    (tmp_path / "workflow.toml").write_text(workflow, encoding="utf-8")
    with pytest.raises(config.ConfigError) as info:
        config.load_profile(tmp_path)
    assert problem in str(info.value)


def test_core_path_rejects_traversal_dot_and_symlinks(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    for path in (".", "../outside", "linked/core"):
        with pytest.raises(config.ConfigError):
            (tmp_path / "workflow.toml").write_text(
                minimal_workflow().replace('path = "vendor/loop-zero"', f'path = "{path}"')
            )
            config.load_profile(tmp_path)
