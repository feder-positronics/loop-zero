from pathlib import Path
import tomllib

import pytest

from conftest import REVISION, git, minimal_workflow
from loopzero import config


def test_minimal_profile_loads(consumer: Path):
    profile = config.load_profile(consumer)
    assert profile.core_revision == REVISION
    assert profile.checks == {"required": ("tests",), "advisory": (), "scheduled": ("health",)}
    assert profile.hooks == {"worktree_setup": ("make setup",), "acceptance": ("make test",)}
    assert profile.env_prefix == "LOOPZERO"
    assert profile.skills_dir == Path(".cursor/skills")
    assert profile.skill_mirrors == (
        Path(".agents/skills"),
        Path(".agent/skills"),
        Path(".claude/skills"),
    )
    assert profile.state_root_explicit is False
    assert profile.snapshot_version() == (Path(__file__).resolve().parents[2] / "core/VERSION").read_text().strip()


def test_profile_records_explicit_state_root(consumer: Path):
    (consumer / "workflow.toml").write_text(
        minimal_workflow(
            extra='[package]\nenv_prefix = "INTELFLO"\nstate_root = "/var/lib/intelflo"\n'
        ),
        encoding="utf-8",
    )
    profile = config.load_profile(consumer)
    assert profile.state_root == "/var/lib/intelflo"
    assert profile.state_root_explicit is True


def test_profile_loads_custom_skill_layout(consumer: Path):
    (consumer / "workflow.toml").write_text(
        minimal_workflow(
            extra=(
                '[package]\nskills_dir = ".codex/skills"\n'
                'skill_mirrors = [".claude/skills"]\nproduct_name = "Consumer"\n'
                '[toolchain]\nbackend_dir = "server"\n'
                '[toolchain.commands]\ndocs_verify = "just docs"\n'
            )
        ),
        encoding="utf-8",
    )
    profile = config.load_profile(consumer)
    assert profile.skills_dir == Path(".codex/skills")
    assert profile.skill_mirrors == (Path(".claude/skills"),)
    assert profile.toolchain["commands"]["docs_verify"] == "just docs"


@pytest.mark.parametrize("value", ["../skills", "/tmp/skills", "."])
def test_skill_layout_rejects_unsafe_canonical_paths(consumer: Path, value: str):
    (consumer / "workflow.toml").write_text(
        minimal_workflow(extra=f'[package]\nskills_dir = "{value}"\n'),
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="skills_dir"):
        config.load_profile(consumer)


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


def test_annotated_tag_object_is_rejected_as_an_exact_base(consumer: Path):
    git(consumer, "tag", "-a", "reviewed", "-m", "reviewed base")
    tag_object = git(consumer, "rev-parse", "refs/tags/reviewed").strip()
    with pytest.raises(config.ConfigError, match="did not resolve to that exact commit"):
        config.resolve_base(consumer, base=tag_object)


def test_consumer_below_git_top_level_is_rejected(tmp_path: Path):
    repository = tmp_path / "repository"
    consumer = repository / "nested-consumer"
    (consumer / "vendor/loop-zero").mkdir(parents=True)
    (repository / "workflow.toml").write_text(minimal_workflow(), encoding="utf-8")
    (consumer / "workflow.toml").write_text(minimal_workflow(), encoding="utf-8")
    git(repository, "init", "-q", "-b", "main")
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    base = git(repository, "rev-parse", "HEAD").strip()

    profile = config.load_profile(consumer)
    with pytest.raises(config.ConfigError, match="must equal Git top level"):
        config.effective_hooks(profile, base)


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


def test_rendered_core_fields_reject_controls_and_managed_markers(tmp_path: Path):
    (tmp_path / "vendor/loop-zero").mkdir(parents=True)
    injected = minimal_workflow().replace(
        'repository = "https://github.com/feder-positronics/loop-zero"',
        f'repository = """https://example.invalid/core\n{config.MANAGED_BLOCK_END}\n"""',
    )
    (tmp_path / "workflow.toml").write_text(injected, encoding="utf-8")
    with pytest.raises(config.ConfigError) as info:
        config.load_profile(tmp_path)
    assert "[core].repository: control characters are forbidden" in str(info.value)
    assert "[core].repository: managed-block marker text is forbidden" in str(info.value)

    data = tomllib.loads(minimal_workflow())
    data["core"]["path"] = "vendor/loop-zero\x01"
    with pytest.raises(config.ConfigError, match=r"\[core\]\.path: control characters"):
        config.validate(data, tmp_path)

    data = tomllib.loads(minimal_workflow())
    data["core"]["path"] = f"vendor/{config.MANAGED_BLOCK_END}"
    with pytest.raises(config.ConfigError, match=r"\[core\]\.path: managed-block marker"):
        config.validate(data, tmp_path)


def test_core_path_rejects_a_missing_component(tmp_path: Path):
    (tmp_path / "workflow.toml").write_text(minimal_workflow(), encoding="utf-8")
    with pytest.raises(
        config.ConfigError, match=r"\[core\]\.path: component .* does not exist"
    ):
        config.load_profile(tmp_path)
