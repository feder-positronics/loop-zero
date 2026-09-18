"""Owned worktree helpers: create, inspect and remove `lz/<slug>` worktrees."""

from __future__ import annotations

import re
from pathlib import Path

from loopzero._proc import run

GIT_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK")
EXCLUDES = (".worktrees/", ".loopzero/")
_BASE_LINE = re.compile(r"^Base:\s*([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)
_TASK_TEMPLATE = """# Task: {slug}

## Objective

(what must change and why)

## Acceptance

- [ ] (observable criteria)

Base: {base}
"""


class WorktreeError(Exception):
    """Raised for refused operations or failing git commands."""


class GitError(WorktreeError):
    def __init__(self, command: tuple[str, ...], tail: str) -> None:
        self.command = command
        self.tail = tail
        super().__init__(f"{' '.join(command)} failed: {tail.strip()[-400:]}")


def _git(cwd: Path, *args: str, timeout: float = 120) -> str:
    argv = ("git", *args)
    done = run(list(argv), cwd=cwd, env_allowlist=GIT_ENV, timeout=timeout)
    if done.exit_code != 0:
        raise GitError(argv, done.stderr or done.stdout)
    return done.stdout.strip()


def branch_name(repo_root: Path, slug: str) -> str:
    """Return `lz/<slug>` after git itself confirms it is a well-formed branch name."""
    if not slug or "/" in slug or slug.startswith("."):
        raise WorktreeError(f"invalid slug: {slug!r}")
    name = f"lz/{slug}"
    done = run(["git", "check-ref-format", "--branch", name], cwd=repo_root,
               env_allowlist=GIT_ENV, timeout=30)
    if done.exit_code != 0:
        raise WorktreeError(f"invalid slug: {slug!r} ({done.stderr.strip()})")
    return name


def worktree_dir(repo_root: Path, slug: str) -> Path:
    return Path(repo_root) / ".worktrees" / slug


def task_file(wt: Path) -> Path:
    return Path(wt) / ".loopzero" / "task.md"


def _branch_exists(repo_root: Path, branch: str) -> bool:
    done = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
        env_allowlist=GIT_ENV,
        timeout=30,
    )
    return done.exit_code == 0


def _ensure_excludes(repo_root: Path) -> None:
    common = Path(_git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    lines = set(existing.splitlines())
    missing = [e for e in EXCLUDES if e not in lines]
    if missing:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + sep + "\n".join(missing) + "\n")


def start(repo_root: Path, slug: str, base_branch: str) -> Path:
    """Create branch `lz/<slug>` from `origin/<base_branch>` in a new worktree."""
    repo_root = Path(repo_root)
    branch = branch_name(repo_root, slug)
    wt = worktree_dir(repo_root, slug)
    if wt.exists():
        raise WorktreeError(f"worktree directory already exists: {wt}")
    if _branch_exists(repo_root, branch):
        raise WorktreeError(f"branch already exists: {branch}")
    _git(repo_root, "fetch", "--quiet", "origin", base_branch)
    base = _git(repo_root, "rev-parse", f"origin/{base_branch}")
    _ensure_excludes(repo_root)
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "--quiet", "-b", branch, str(wt), base)
    task = task_file(wt)
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text(_TASK_TEMPLATE.format(slug=slug, base=base))
    return wt


def head(wt: Path) -> str:
    return _git(Path(wt), "rev-parse", "HEAD")


def branch(wt: Path) -> str:
    name = _git(Path(wt), "symbolic-ref", "--quiet", "--short", "HEAD")
    if not name:
        raise WorktreeError(f"detached HEAD in {wt}")
    return name


def task_text(wt: Path) -> str:
    task = task_file(wt)
    if not task.exists():
        raise WorktreeError(f"missing {task}")
    return task.read_text()


def base_sha(wt: Path) -> str:
    match = _BASE_LINE.search(task_text(wt))
    if not match:
        raise WorktreeError(f"no 'Base: <sha>' line in {task_file(wt)}")
    return match.group(1)


def is_dirty(wt: Path) -> bool:
    return bool(_git(Path(wt), "status", "--porcelain", "--untracked-files=all"))


def diff_since(wt: Path, sha: str) -> str:
    return _git(Path(wt), "diff", "--no-color", sha, "HEAD")


def cleanup(repo_root: Path, wt: Path) -> None:
    """Remove the worktree and its branch. Refuses when the worktree is dirty."""
    repo_root, wt = Path(repo_root), Path(wt)
    if not wt.exists():
        raise WorktreeError(f"no such worktree: {wt}")
    if is_dirty(wt):
        raise WorktreeError(f"worktree is dirty, refusing to remove: {wt}")
    name = branch(wt)
    _git(repo_root, "worktree", "remove", str(wt))
    _git(repo_root, "branch", "-D", name)
