"""Owned worktree helpers: create, inspect and remove `lz/<slug>` worktrees."""

from __future__ import annotations

import errno
import re
import tempfile
from pathlib import Path

from loopzero._proc import run

GIT_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK")
EXCLUDES = (".worktrees/", ".loopzero/")
_BASE_LINE = re.compile(r"^Base:\s*([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)
_TASK_TEMPLATE = """# {title}

## Context and goal
- **Goal:** <one plain sentence: what the owner or users now notice (and cost/saving if known), because this change fixes the problem>
- **Problem:** <one or two plain sentences before code names: what goes wrong (one problem only), who notices, and evidence/cost; refs in brackets at the end>
- **Context:** <plain newcomer context; explain domain terms from read sources or describe what they do instead; rulings and dispatch belong in Notes>

Refs #<issue number, or remove this line>

## Acceptance
- <observable behaviour, e.g. "a new plan opens by restating the ask"; not wording, e.g. "plan/SKILL.md line 12 says X">

## Notes

## Base
{base_branch} @ {base}
Base: {base}

## Validation
(filled by `loopzero check`)

## Review
(filled by `loopzero review`)
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


def title_from_slug(slug: str) -> str:
    """`add-workflow_config-note` -> `Add workflow config note`."""
    words = re.sub(r"[-_]+", " ", slug).split()
    return " ".join(words)[:1].upper() + " ".join(words)[1:]


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
    """Create a task worktree or finish initializing an interrupted start."""
    repo_root = Path(repo_root)
    branch = branch_name(repo_root, slug)
    wt = worktree_dir(repo_root, slug)
    task = task_file(wt)
    existing = wt.exists()
    refusal = f"worktree directory already exists: {wt}"
    if wt.is_symlink() or task.exists() or task.is_symlink() or task.parent.is_symlink():
        raise WorktreeError(refusal)
    if not existing and _branch_exists(repo_root, branch):
        raise WorktreeError(f"branch already exists: {branch}")
    _git(repo_root, "fetch", "--quiet", "origin", base_branch)
    base_ref = f"origin/{base_branch}"
    base = _git(repo_root, "rev-parse", base_ref)
    if existing:
        existing_head = _resumable_head(repo_root, wt, branch)
        if existing_head is None:
            raise WorktreeError(refusal)
        if existing_head != base:
            raise WorktreeError(
                f"cannot resume start at {wt}: existing HEAD {existing_head} does not match "
                f"{base_ref} at {base}; run loopzero start with a new slug"
            )
    _ensure_excludes(repo_root)
    if not existing:
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(repo_root, "worktree", "add", "--quiet", "-b", branch, str(wt), base)
    _initialize_task(task, _TASK_TEMPLATE.format(
        title=title_from_slug(slug), base_branch=base_branch, base=base))
    return wt


def _resumable_head(repo_root: Path, wt: Path, expected_branch: str) -> str | None:
    registered = _git(repo_root, "worktree", "list", "--porcelain", "-z").split("\0\0")
    path_field = f"worktree {wt.resolve()}"
    entry = next((item for item in registered if item.startswith(f"{path_field}\0")), "")
    fields = entry.split("\0")
    if (len(fields) != 3 or fields[2] != f"branch refs/heads/{expected_branch}"
            or not (wt / ".git").is_file()):
        return None
    common_dir = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if _git(wt, *common_dir) != _git(repo_root, *common_dir) or branch(wt) != expected_branch:
        return None
    current_head = head(wt)
    if fields[1] != f"HEAD {current_head}" or is_dirty(wt):
        return None
    return current_head


def _initialize_task(task: Path, text: str) -> None:
    if task.parent.is_symlink():
        raise WorktreeError(f"task directory is a symbolic link: {task.parent}")
    task.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=task.parent, prefix=".task.md-", delete=False
    ) as stream:
        temp = Path(stream.name)
        try:
            stream.write(text)
            stream.close()
            # Publish the complete file without replacing an existing handoff.
            try:
                task.hardlink_to(temp)
            except FileExistsError as exc:
                raise WorktreeError(f"task file already exists: {task}") from exc
            except OSError as exc:
                if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS, errno.EPERM):
                    raise
                raise WorktreeError(
                    f"cannot publish task file {task} ({exc.strerror}): hard links are required; "
                    "run loopzero start from a checkout on a filesystem that supports hard links"
                ) from exc
        finally:
            temp.unlink(missing_ok=True)


def head(wt: Path) -> str:
    return _git(Path(wt), "rev-parse", "HEAD")


def branch(wt: Path) -> str:
    name = _git(Path(wt), "branch", "--show-current")
    if not name:
        raise WorktreeError("HEAD is detached. Switch to a branch and retry.")
    return name


def task_text(wt: Path) -> str:
    task = task_file(wt)
    if not task.exists():
        raise WorktreeError(
            f"missing {task}. Run `loopzero start <slug>` for a new task worktree, "
            "or recreate the file using the handoff template at "
            "https://github.com/feder-positronics/loop-zero/blob/main/core/HANDOFF.md"
        )
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


def merges_since(wt: Path, sha: str) -> list[str]:
    """First-parent merges between ``sha`` and HEAD, oldest first."""
    return _git(Path(wt), "rev-list", "--first-parent", "--reverse", "--merges", f"{sha}..HEAD").split()


def cleanup(wt: Path) -> None:
    """Remove the worktree and its branch. Refuses when the worktree is dirty."""
    wt = Path(wt).absolute()
    if not wt.exists():
        raise WorktreeError(f"no such worktree: {wt}")
    if is_dirty(wt):
        raise WorktreeError(f"worktree is dirty, refusing to remove: {wt}")
    name, gd = branch(wt), (wt / _git(wt, "rev-parse", "--git-common-dir")).resolve()
    _git(gd, f"--git-dir={gd}", "worktree", "remove", str(wt))  # never the primary checkout
    try:
        _git(gd, f"--git-dir={gd}", "branch", "-D", name)
    except GitError:
        if _git(gd, f"--git-dir={gd}", "branch", "--list", "--", name):
            raise
