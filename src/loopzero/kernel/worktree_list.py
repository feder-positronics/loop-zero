"""List every worktree registered for this repo with age, branch, dirty state, and tool tag.

Covers Claude Code worktrees (`<repo>/.claude/worktrees/<name>/`), Codex/Cursor
BG-agent worktrees (`~/.cursor/worktrees/...`), and manual `git worktree add`
locations. Shows the primary checkout for context.

The companion `worktree_prune.py` is intentionally narrower — it only prunes
under `<repo>/.claude/worktrees/` because that's the lifecycle this repo's
make targets own. Cursor-managed worktrees are cleaned by the Cursor harness.
"""

from __future__ import annotations

import subprocess
from .settings import settings
import sys
import time
from pathlib import Path


def repo_root() -> Path | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return Path(out) if out else None
    except subprocess.CalledProcessError:
        return None


def parse_worktrees(root: Path) -> list[tuple[Path, str]]:
    """Return [(path, branch_or_detached)] for every worktree registered to this repo."""
    out = subprocess.check_output(["git", "worktree", "list", "--porcelain"], text=True)
    entries: list[tuple[Path, str]] = []
    current_path: Path | None = None
    current_branch: str = "(detached)"
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                entries.append((current_path, current_branch))
            current_path = Path(line[len("worktree ") :])
            current_branch = "(detached)"
        elif line.startswith("branch "):
            current_branch = line[len("branch ") :].removeprefix("refs/heads/")
    if current_path is not None:
        entries.append((current_path, current_branch))
    return entries


def tool_tag(path: Path, root: Path) -> str:
    """Classify the worktree by which agent harness owns its lifecycle."""
    s = str(path)
    if path == root:
        return "primary"
    for prefix, tag in settings.worktree_tags.items():
        expanded = Path(prefix).expanduser()
        candidate = expanded if expanded.is_absolute() else root / expanded
        if path.is_relative_to(candidate):
            return tag
    for marker, tag in settings.worktree_markers.items():
        if marker in s:
            return tag
    return "external"


def dirty_count(path: Path) -> int:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return -1
    return sum(1 for line in out.splitlines() if line.strip())


def age_days(path: Path) -> int:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return -1
    return int((time.time() - mtime) // 86400)


def main() -> int:
    root = repo_root()
    if root is None:
        print("Not in a git repository.", file=sys.stderr)
        return 1

    print("🌳 Worktrees registered for this repo:")
    entries = parse_worktrees(root)
    if not entries:
        print("  (none)")
        return 0

    home = Path.home()
    for path, branch in entries:
        if path.is_relative_to(root):
            display = str(path.relative_to(root))
        elif path.is_relative_to(home):
            display = "~/" + str(path.relative_to(home))
        else:
            display = str(path)
        tag = tool_tag(path, root)
        age = age_days(path)
        dirty = dirty_count(path)
        print(
            f"  [{tag:<11}] {display:<55}  branch={branch:<30}  age={age}d  dirty={dirty}"
        )
    print()
    print("Lifecycle: `claude-code` worktrees are pruned by `make worktree-prune`.")
    print("`cursor/codex` worktrees are owned by the Cursor harness — clean them there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
