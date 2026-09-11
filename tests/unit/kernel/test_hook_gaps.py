"""Behavior tests for every hook without imported dedicated coverage."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[3] / "src/loopzero/hooks"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Kernel Test")
    git(tmp_path, "config", "user.email", "kernel@example.invalid")
    (tmp_path / "base").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    return tmp_path


def hook(name, root, *, env=None, payload=None):
    return subprocess.run(["bash", str(HOOKS / name)], cwd=root,
                          env={**os.environ, **(env or {})},
                          input=json.dumps(payload) if payload is not None else "",
                          capture_output=True, text=True, timeout=20)


def test_primary_collision_blocks_multiple_staged_source_files(repo, tmp_path):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "other", str(linked))
    for name in ("one.py", "two.py"):
        (repo / name).write_text("x = 1\n")
    git(repo, "add", "one.py", "two.py")
    assert hook("primary-collision-check.sh", repo).returncode == 1
    assert hook("primary-collision-check.sh", repo, env={"FOOTGUN_BYPASS": "1"}).returncode == 0


def test_bash_guard_warns_on_unqualified_stash_pop(repo):
    result = hook("agent-bash-guard.sh", repo, payload={"tool_input": {"command": "git stash pop"}})
    assert result.returncode == 0
    assert "stash" in result.stdout and "warning" in result.stdout


def test_staging_guard_refuses_outdated_protected_branch(repo):
    old = git(repo, "rev-parse", "HEAD")
    (repo / "base").write_text("new\n")
    git(repo, "add", "base")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "new")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--soft", old)
    result = hook("staging-base-check.sh", repo)
    assert result.returncode == 1
    assert "behind" in result.stderr


def test_commit_author_uses_injected_namespace_and_identity(repo):
    env = {"LOOPZERO_ENV_PREFIX": "ACME", "ACME_COMMIT_AUTHOR_NAME": "Kernel Test",
           "ACME_COMMIT_AUTHOR_EMAIL": "kernel@example.invalid"}
    assert hook("commit-author-check.sh", repo, env=env).returncode == 0
    env["ACME_COMMIT_AUTHOR_EMAIL"] = "different@example.invalid"
    assert hook("commit-author-check.sh", repo, env=env).returncode == 1


def test_canonical_skill_hook_rejects_copied_skill_content(repo):
    path = repo / ".claude/skills/example/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("copied skill")
    git(repo, "add", ".claude")
    result = hook("check-skills-canonical-dir.sh", repo)
    assert result.returncode == 1
    assert "SKILL.md" in result.stdout


def test_pytest_config_hook_rejects_nested_plugin_declaration(repo):
    path = repo / "tests/unit/conftest.py"
    path.parent.mkdir(parents=True)
    path.write_text("pytest_plugins = ['example']\n")
    result = hook("pytest-config-check.sh", repo, env={"LOOPZERO_PYTEST_ROOT": str(repo), "LOOPZERO_PYTHON": sys.executable})
    assert result.returncode == 1
    assert "pytest_plugins" in result.stdout
