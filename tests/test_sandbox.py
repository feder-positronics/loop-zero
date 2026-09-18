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
for a in "$@"; do printf '%s\\n' "$a" >> {log}; done
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
    """argv of the last bwrap invocation (the probe runs first, checks overwrite it)."""
    return log.read_text().splitlines()


def test_report_head_dirty_and_results(git_repo, bwrap_log):
    report = sandbox.run_checks(make_config(checks=("echo one", "echo two; exit 4")), git_repo)
    assert report.head == git(git_repo, "rev-parse", "HEAD").strip()
    assert report.dirty is False
    (git_repo / "README.md").write_text("changed\n")
    assert sandbox.run_checks(make_config(), git_repo).dirty is True
    assert [(r.command, r.exit_code, r.tail) for r in report.results] == [
        ("echo one", 0, "one"),
        ("echo two; exit 4", 4, "two"),
    ]
    assert report.ok is False


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
    system = [p for p in sandbox.SYSTEM_RO if Path(p).exists()]
    assert _pairs(head, "--ro-bind") == [(p, p) for p in system] + [(str(git_repo),) * 2]
    assert "/" not in system and "/home" not in system
    assert _pairs(head, "--ro-bind-try") == []
    windows = list(itertools.pairwise(head))
    assert all(("--tmpfs", p) in windows for p in ("/tmp", "/run", "/var/run"))
    assert ("--dev", "/dev") in windows and ("--proc", "/proc") in windows
    assert head[head.index("--chdir") + 1] == str(git_repo)
    setenv = dict(_pairs(head, "--setenv"))
    assert setenv["PATH"] == os.environ["PATH"]
    assert setenv["LZ_KEEP"] == "kept"
    assert "LZ_DROP" not in setenv
    assert setenv["HOME"] == sandbox.SANDBOX_HOME
    assert setenv["UV_CACHE_DIR"] == "/tmp/uv-cache" and setenv["PYTHONDONTWRITEBYTECODE"] == "1"
    binds = _pairs(head, "--bind")
    home_src, home_dst = binds[0]
    assert home_dst == sandbox.SANDBOX_HOME and Path(home_src).name.startswith("loopzero-home-")
    assert Path(home_src) != Path(os.environ.get("HOME", "/"))
    # Scratch dirs: per-run tmp sources under the home dir, bound after the ro worktree bind.
    scratch = {dst: src for src, dst in binds[1:]}
    assert list(scratch) == [str(git_repo / e) for e in make_config().scratch]
    assert all(src.startswith(home_src + "/scratch/") for src in scratch.values())
    assert head.index(str(git_repo)) < head.index(str(git_repo / ".venv"))
    assert (git_repo / "node_modules/.cache").is_dir()
    assert sandbox.run_checks(make_config(), git_repo).dirty is False
    # /tmp tmpfs comes before the worktree bind so a worktree under /tmp stays visible.
    assert head.index("--tmpfs") < head.index(str(git_repo))
    assert tail == ["/bin/sh", "-c", "echo hello"]


def test_probe_uses_real_flags(git_repo, bwrap_log):
    sandbox.run_checks(make_config(checks=(), network=True), git_repo)
    argv = argv_of(bwrap_log)  # only the probe ran
    assert argv[argv.index("--") + 1 :] == ["true"]
    assert argv.index("--unshare-all") < argv.index("--share-net") < argv.index("--clearenv")


def test_extra_paths_and_env_override(git_repo, bwrap_log, tmp_path, monkeypatch):
    extra, cache = tmp_path / "ro", tmp_path / "rw"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    cfg = make_config(sandbox_ro=(str(extra),), writable=(str(cache),), scratch=())
    sandbox.run_checks(Config(**{**cfg.__dict__, "env_allowlist": ("PATH", "UV_CACHE_DIR")}), git_repo)
    argv = argv_of(bwrap_log)
    assert _pairs(argv, "--ro-bind-try") == [(str(extra),) * 2]
    assert _pairs(argv, "--bind-try") == [(str(cache),) * 2]
    assert len(_pairs(argv, "--bind")) == 1  # only HOME; no scratch
    assert dict(_pairs(argv, "--setenv"))["UV_CACHE_DIR"] == str(cache)


def test_timeout_is_recorded_not_raised(git_repo, bwrap_log):
    report = sandbox.run_checks(
        make_config(checks=("echo partial; sleep 5", "echo after")), git_repo, timeout=0.3
    )
    first, second = report.results
    assert (first.exit_code, first.tail.splitlines()[0]) == (sandbox.TIMEOUT_EXIT, "partial")
    assert "timed out" in first.tail and not report.ok
    assert (second.exit_code, second.tail) == (0, "after")


def test_linked_worktree_binds_common_git_dir(git_repo, bwrap_log, tmp_path):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    sandbox.run_checks(make_config(), linked)
    binds = _pairs(argv_of(bwrap_log), "--ro-bind")
    common = (git_repo / ".git").resolve()
    assert binds[-2:] == [(str(linked.resolve()),) * 2, (str(common),) * 2]


def test_bwrap_missing_or_probe_failure(git_repo, fake_bin, fake_tool, monkeypatch):
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(fake_bin))
    with pytest.raises(sandbox.SandboxUnavailable, match="bwrap not found"):
        sandbox.run_checks(make_config(), git_repo)
    monkeypatch.setenv("PATH", path)
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
    probe = ["bwrap", "--unshare-all", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    probe += [a for p in ("/usr", "/bin", "/lib", "/lib64") for a in ("--ro-bind", p, p)] + ["true"]
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


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_hides_host_sockets_and_homes(git_repo):
    uid = os.getuid()
    checks = (
        f"test ! -e /run/user/{uid}",
        "test ! -e /var/run/docker.sock && test ! -e /run/docker.sock",
        "test ! -e /home && test ! -e /root",
        "test -z \"$(ls -A /run 2>/dev/null)\"",
        "test -d /usr && test -d /etc && test -w /tmp",
        'test "$(cat /proc/1/comm)" = bwrap',  # private pid namespace: pid 1 is not host init
    )
    report = sandbox.run_checks(make_config(checks=checks), git_repo)
    assert [r.exit_code for r in report.results] == [0] * len(checks), report


@pytest.mark.skipif(
    not _real_bwrap_works() or shutil.which("uv") is None, reason="needs real bwrap and uv"
)
def test_real_sandbox_uv_run_offline(git_repo, monkeypatch):
    home = Path.home()
    (git_repo / "pyproject.toml").write_text(
        '[project]\nname = "t"\nversion = "0"\nrequires-python = ">=3.12"\n'
        "[dependency-groups]\ndev = []\n"
    )
    monkeypatch.setenv("UV_CACHE_DIR", str(home / ".cache/uv"))
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(home / ".local/share/uv/python"))
    subprocess.run(["uv", "lock", "-q"], cwd=git_repo, check=True)  # projects commit uv.lock
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "uv project")
    cfg = make_config(
        checks=("uv run --group dev python -c 1 && test -e .venv/bin/python && test ! -w .",),
        env_allowlist=("PATH", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"),
        sandbox_ro=(str(home / ".local/bin"), str(home / ".local/share/uv")),
        writable=(str(home / ".cache/uv"),),
    )
    (result,) = sandbox.run_checks(cfg, git_repo).results
    assert result.exit_code == 0, result.tail
    assert not (git_repo / ".venv/bin").exists()  # venv lived in the scratch dir


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]
