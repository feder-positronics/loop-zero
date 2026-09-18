from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loopzero import sandbox
from loopzero.types import Config

from .conftest import git

FAKE_BWRAP = """#!/bin/sh
: > {log}
found=
for a in "$@"; do printf '%s\\n' "$a" >> {log}; [ "$a" = -- ] && found=1; done
[ -n "$found" ] || exit 0  # the availability probe has no separator
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
"""


def make_config(**overrides) -> Config:
    values = {
        "repo": "o/n",
        "base_branch": "main",
        "checks": ("echo hello",),
        "required_ci": (),
        "merge_strategy": "squash",
        "reviewers": ("claude",),
        "env_allowlist": ("PATH", "HOME", "LZ_KEEP"),
    }
    return Config(**{**values, **overrides})


@pytest.fixture
def bwrap_log(fake_tool, tmp_path) -> Path:
    log = tmp_path / "bwrap.argv"
    fake_tool("bwrap", FAKE_BWRAP.format(log=log))
    return log


def argv_of(log: Path) -> list[str]:
    return log.read_text().splitlines()


def test_report_head_dirty_and_results(git_repo, bwrap_log):
    report = sandbox.run_checks(make_config(checks=("echo one", "echo two; exit 4")), git_repo)
    assert report.head == git(git_repo, "rev-parse", "HEAD").strip()
    assert report.dirty is False
    assert [(r.command, r.exit_code, r.tail) for r in report.results] == [
        ("echo one", 0, "one"),
        ("echo two; exit 4", 4, "two"),
    ]
    assert report.ok is False
    assert all(r.duration_s >= 0 for r in report.results)


def test_dirty_worktree_is_reported(git_repo, bwrap_log):
    (git_repo / "README.md").write_text("changed\n")
    assert sandbox.run_checks(make_config(), git_repo).dirty is True


def test_tail_merges_streams_and_truncates(git_repo, bwrap_log):
    cmd = "for i in $(seq 1 50); do echo $i; done; echo err >&2"
    (result,) = sandbox.run_checks(make_config(checks=(cmd,)), git_repo).results
    lines = result.tail.splitlines()
    assert len(lines) == 40
    assert lines[0] == "12"
    assert lines[-1] == "err"


def test_bwrap_flags(git_repo, bwrap_log, monkeypatch):
    monkeypatch.setenv("LZ_KEEP", "kept")
    monkeypatch.setenv("LZ_DROP", "dropped")
    sandbox.run_checks(make_config(), git_repo)
    argv = argv_of(bwrap_log)
    head, tail = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    for flag in ("--die-with-parent", "--new-session", "--unshare-all", "--clearenv"):
        assert flag in head
    assert "--share-net" not in head
    assert head[head.index("--cap-drop") + 1] == "ALL"
    assert _pairs(head, "--ro-bind") == [("/", "/"), (str(git_repo), str(git_repo))]
    assert ("--tmpfs", "/tmp") in _windows(head)
    assert head[head.index("--chdir") + 1] == str(git_repo)
    setenv = dict(_pairs(head, "--setenv"))
    assert setenv["PATH"] == os.environ["PATH"]
    assert setenv["LZ_KEEP"] == "kept"
    assert "LZ_DROP" not in setenv
    assert setenv["HOME"] == sandbox.SANDBOX_HOME
    assert setenv["HOME"] != os.environ["HOME"]
    bind = dict(_pairs(head, "--bind"))
    assert bind and list(bind.values()) == [sandbox.SANDBOX_HOME]
    assert Path(next(iter(bind))).name.startswith("loopzero-home-")
    # /tmp tmpfs comes before the worktree bind so a worktree under /tmp stays visible.
    assert head.index("--tmpfs") < head.index(str(git_repo))
    assert tail == ["/bin/sh", "-c", "echo hello"]


def test_network_flag(git_repo, bwrap_log):
    sandbox.run_checks(make_config(network=True), git_repo)
    argv = argv_of(bwrap_log)
    assert argv.index("--unshare-all") < argv.index("--share-net")


def test_linked_worktree_binds_common_git_dir(git_repo, bwrap_log, tmp_path):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    sandbox.run_checks(make_config(), linked)
    binds = _pairs(argv_of(bwrap_log), "--ro-bind")
    common = (git_repo / ".git").resolve()
    assert binds == [("/", "/"), (str(linked.resolve()),) * 2, (str(common),) * 2]


def test_bwrap_missing(git_repo, fake_bin, monkeypatch):
    monkeypatch.setenv("PATH", str(fake_bin))
    with pytest.raises(sandbox.SandboxUnavailable, match="bwrap not found"):
        sandbox.run_checks(make_config(), git_repo)


def test_bwrap_probe_failure(git_repo, fake_tool):
    fake_tool("bwrap", "echo 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    with pytest.raises(sandbox.SandboxUnavailable, match="No permissions to create new namespace"):
        sandbox.run_checks(make_config(), git_repo)


def test_not_a_git_repo(tmp_path, bwrap_log):
    with pytest.raises(sandbox.SandboxUnavailable, match="git rev-parse HEAD failed"):
        sandbox.run_checks(make_config(), tmp_path)


def _real_bwrap_works() -> bool:
    # Hosts with AppArmor or no unprivileged user namespaces cannot run bwrap; skip there.
    if shutil.which("bwrap") is None:
        return False
    probe = ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"]
    try:
        return subprocess.run(probe, capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_denies_writes(git_repo):
    checks = (
        "touch escaped.txt",
        "touch .git/escaped",
        "echo x > \"$HOME/ok\" && cat \"$HOME/ok\" && test ! -e /tmp/repo",
        "test \"$(pwd)\" = \"$PWD\" && env | grep -c '^LZ_' ; true",
    )
    report = sandbox.run_checks(make_config(checks=checks), git_repo)
    codes = [r.exit_code for r in report.results]
    assert codes[0] != 0 and codes[1] != 0, report
    assert codes[2] == 0, report
    assert not (git_repo / "escaped.txt").exists()
    assert not (git_repo / ".git" / "escaped").exists()
    assert report.head


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def _windows(argv: list[str]) -> list[tuple[str, str]]:
    return list(itertools.pairwise(argv))
