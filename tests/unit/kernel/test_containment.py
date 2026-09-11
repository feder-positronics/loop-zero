"""Validation children cannot acquire the parent's commit authority."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from loopzero.kernel import sandbox
from .capabilities import NAMESPACE_AVAILABLE, NAMESPACE_REASON


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", *args], text=True,
    ).strip()


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
@pytest.mark.parametrize("linked_child", [False, True])
def test_validation_child_cannot_write_parent_git_or_inherit_authority(tmp_path, linked_child):
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-q", "-b", "main")
    (parent / "tracked").write_text("base\n")
    git(parent, "add", ".")
    git(parent, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    linked = tmp_path / "linked"
    git(parent, "worktree", "add", "-qb", "linked", str(linked))
    before = git(parent, "show-ref")
    common = Path(git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    targets = [parent / ".git" / "validation-probe", common / "refs" / "validation-probe"]
    script = '''import json, os, subprocess, sys
from pathlib import Path
results = []
for name in sys.argv[1:]:
    try:
        Path(name).write_text("forged")
        results.append(False)
    except OSError:
        results.append(True)
assert all(results), results
assert not any("LEASE" in name or "NONCE" in name for name in os.environ)
# Grandchildren inherit the same mount and environment boundary.
assert subprocess.call(["/bin/sh", "-c", 'test -z "$(env | grep -E "LEASE|NONCE")"']) == 0
print(json.dumps(results))
'''
    result = sandbox.run_validation_child(
        ["/usr/bin/python3", "-c", script, *map(str, targets)],
        worktree=linked if linked_child else parent,
        source_environment={**os.environ, "INTELFLO_WORKTREE_LEASE_NONCE": "secret",
                            "INTELFLO_WORKTREE_LEASE_FD": "17",
                            "OTHER_REPO_LEASE": "secret", "CUSTOM_NONCE": "secret"},
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, True]
    assert git(parent, "show-ref") == before
    assert all(not target.exists() for target in targets)


def test_validation_environment_excludes_authority_aliases():
    env = sandbox.environment({"LANG": "C", "PATH": "/candidate/bin",
                               "INTELFLO_WORKTREE_LEASE_NONCE": "secret",
                               "OTHER_REPO_LEASE_FD": "4", "CUSTOM_NONCE": "secret"})
    assert env["LANG"] == "C"
    assert env["PATH"] != "/candidate/bin"
    assert not any("LEASE" in name or "NONCE" in name for name in env)
