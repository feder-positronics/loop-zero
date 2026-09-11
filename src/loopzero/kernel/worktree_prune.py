"""Prune Claude Code worktrees that are clean and older than 7 days.

Safety:
- **Only touches paths inside `<repo>/.claude/worktrees/`.** Cursor/Codex
  worktrees under `~/.cursor/worktrees/` are owned by the Cursor harness
  and are NEVER pruned by this script — clean them via Cursor's UI or
  background-agent management instead. Manual worktrees outside both
  conventions are also skipped.
- Skips worktrees with any staged, modified, or untracked files.
- Skips worktrees younger than the age threshold.
- Uses `git worktree remove` (refuses on dirty state by default).
"""

from __future__ import annotations

import argparse
from .settings import settings
import subprocess
import sys
from pathlib import Path


from .worktree_list import age_days, dirty_count, parse_worktrees, repo_root, tool_tag

DEFAULT_AGE_DAYS = 7


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=DEFAULT_AGE_DAYS,
        help=f"Only prune worktrees older than this many days (default: {DEFAULT_AGE_DAYS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without removing anything.",
    )
    args = parser.parse_args()

    root = repo_root()
    if root is None:
        print("Not in a git repository.", file=sys.stderr)
        return 1

    print(
        f"🌳 Pruning stale Claude Code worktrees "
        f"(clean, >{args.min_age_days} days old, under .claude/worktrees/)..."
    )
    entries = parse_worktrees(root)
    pruned = 0
    skipped_dirty = 0
    skipped_foreign = 0
    for path, _branch in entries:
        tag = tool_tag(path, root)
        if tag == "primary":
            # Never touch the primary checkout.
            continue
        if tag not in settings.prunable_worktree_tags:
            # Lifecycle is not ours — never auto-remove Cursor/Codex/manual worktrees.
            skipped_foreign += 1
            continue
        age = age_days(path)
        if age < args.min_age_days:
            continue
        dirty = dirty_count(path)
        if dirty != 0:
            print(f"  SKIP {path} (dirty={dirty}, age={age}d)")
            skipped_dirty += 1
            continue
        if args.dry_run:
            print(f"  WOULD REMOVE {path} (clean, age={age}d)")
            pruned += 1
            continue
        print(f"  REMOVE {path} (clean, age={age}d)")
        try:
            subprocess.run(
                ["git", "worktree", "remove", str(path)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            pruned += 1
        except subprocess.CalledProcessError as exc:
            print(f"    (remove failed: {exc})")

    print(
        f"✅ Pruned {pruned} Claude Code worktree(s); "
        f"skipped {skipped_dirty} dirty, {skipped_foreign} foreign "
        f"(Cursor/Codex/manual — owned elsewhere)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
