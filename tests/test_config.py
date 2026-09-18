from __future__ import annotations

import pytest

from loopzero import config
from loopzero.types import Config

FULL = """
[repo]
name = "owner/name"
base = "develop"

[checks]
commands = ["uv run pytest -q", "uv run ruff check ."]
required_ci = ["checks"]
network = true
env_allowlist = ["PATH", "HOME"]

[delivery]
merge = "rebase"
reviewers = ["codex"]
"""


def write(tmp_path, text):
    path = tmp_path / "workflow.toml"
    path.write_text(text)
    return path


def test_load_full_config(tmp_path):
    assert config.load(write(tmp_path, FULL)) == Config(
        repo="owner/name",
        base_branch="develop",
        checks=("uv run pytest -q", "uv run ruff check ."),
        required_ci=("checks",),
        merge_strategy="rebase",
        reviewers=("codex",),
        network=True,
        env_allowlist=("PATH", "HOME"),
    )


def test_load_applies_defaults(tmp_path):
    loaded = config.load(write(tmp_path, '[repo]\nname = "o/n"\n'))
    assert loaded == Config(
        repo="o/n",
        base_branch="main",
        checks=(),
        required_ci=(),
        merge_strategy="squash",
        reviewers=("claude", "codex"),
    )
    assert loaded.network is False
    assert loaded.env_allowlist == ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


def test_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="config file not found"):
        config.load(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path):
    with pytest.raises(config.ConfigError, match="invalid TOML"):
        config.load(write(tmp_path, "[repo\n"))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[checks]\ncommands = []\n", "missing required key repo.name"),
        ('[repo]\nname = "noslash"\n', 'must look like "owner/name"'),
        ('[repo]\nname = 3\n', "repo.name must be a str"),
        ('[repo]\nname = "o/n"\n[checks]\ncommands = "pytest"\n', "checks.commands must be a list"),
        ('[repo]\nname = "o/n"\n[checks]\ncommands = ["ok", 1]\n', "non-empty strings"),
        ('[repo]\nname = "o/n"\n[checks]\nnetwork = "yes"\n', "checks.network must be a bool"),
        ('[repo]\nname = "o/n"\n[delivery]\nmerge = "ff"\n', "delivery.merge must be one of"),
        ('[repo]\nname = "o/n"\n[delivery]\nreviewers = ["gpt"]\n', "delivery.reviewers entries"),
        ('[repo]\nname = "o/n"\n[repo2]\nx = 1\n', "unknown key\\(s\\) in top level: repo2"),
        ('[repo]\nname = "o/n"\nnmae = "x"\n', "unknown key\\(s\\) in \\[repo\\]: nmae"),
        ('repo = "o/n"\n', "\\[repo\\] must be a table"),
    ],
)
def test_invalid_content(tmp_path, text, message):
    with pytest.raises(config.ConfigError, match=message) as info:
        config.load(write(tmp_path, text))
    assert str(info.value).startswith(str(tmp_path / "workflow.toml"))
