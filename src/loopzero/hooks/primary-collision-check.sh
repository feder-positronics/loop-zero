#!/usr/bin/env bash
# Primary-checkout collision guard (pre-commit stage).
#
# Blocks a commit made in the PRIMARY checkout while sibling worktrees are
# active and the change is not a codified exemption — the pre-hoc counterpart to
# `make footgun-audit` (incident #1026: a concurrent agent switched HEAD in the
# shared primary checkout mid-commit, stranding the commit on the wrong branch).
#
# Allows (exit 0) when ANY of:
#   - FOOTGUN_BYPASS=1 is set (documented escape hatch; pair with a
#     `Footgun-bypass: <reason>` commit footer so `make footgun-audit` sees it)
#   - running inside a worktree (the sanctioned place for mutating work)
#   - a merge/rebase is in progress (don't block conflict-resolution commits)
#   - no sibling worktrees exist (single checkout — no shared-index collision)
#   - the staged scope is a codified exemption: a single file, docs-only, or a
#     meta-edit to the parallel-agents rule itself
#
# Mirrors the established `staging-base-check.sh` pattern: always_run,
# pass_filenames: false, env-var bypass, reads local git state only (no network).
set -euo pipefail

# Explicit, documented bypass.
if [ "${FOOTGUN_BYPASS:-}" = "1" ]; then
    exit 0
fi

git_dir="$(git rev-parse --git-dir)"
common_dir="$(git rev-parse --git-common-dir)"

# Inside a worktree (git-dir != common-dir) → allow. This is where mutating work
# is supposed to happen.
if [ "$git_dir" != "$common_dir" ]; then
    exit 0
fi

# Merge/rebase in progress → don't block resolution commits.
if [ -e "$git_dir/MERGE_HEAD" ] || [ -d "$git_dir/rebase-merge" ] || [ -d "$git_dir/rebase-apply" ]; then
    exit 0
fi

# Count registered worktrees; ≤1 means primary only → no shared-index collision.
worktree_count="$(git worktree list --porcelain | grep -c '^worktree ' || true)"
if [ "${worktree_count:-0}" -le 1 ]; then
    exit 0
fi

mapfile -t staged < <(git diff --cached --name-only)
file_count=${#staged[@]}

# Nothing staged (e.g. an empty/amend edge) → nothing to guard.
if [ "$file_count" -eq 0 ]; then
    exit 0
fi

# Exemption: codified "trivial single-file edit".
if [ "$file_count" -eq 1 ]; then
    exit 0
fi

# Exemption: meta-edit to the parallel-agents rule itself (canonical + mirrors).
meta_only=1
for p in "${staged[@]}"; do
    case "$p" in
        *parallel-agents.mdc | *parallel-agents.md) ;;
        *) meta_only=0; break ;;
    esac
done
if [ "$meta_only" -eq 1 ]; then
    exit 0
fi

# Exemption: docs-only (docs/** or stray *.md), excluding .cursor/ rules/skills.
docs_only=1
for p in "${staged[@]}"; do
    case "$p" in
        .cursor/*) docs_only=0; break ;;
        docs/*) ;;
        *.md) ;;
        *) docs_only=0; break ;;
    esac
done
if [ "$docs_only" -eq 1 ]; then
    exit 0
fi

# Otherwise: block.
sibling_count=$((worktree_count - 1))
{
    echo "✗ Primary-checkout commit blocked: ${sibling_count} sibling worktree(s) active."
    echo ""
    echo "  Mutating multi-file work in the shared primary checkout races concurrent"
    echo "  agents on the git index (incident #1026: HEAD hijack → dangling commit)."
    echo ""
    echo "  Do one of:"
    echo "    • Move this work into a worktree:  EnterWorktree  (preferred)"
    echo "    • Sanctioned exception: set FOOTGUN_BYPASS=1 and add a"
    echo "      'Footgun-bypass: <reason>' footer to the commit message (audit trail)."
    echo ""
    echo "  Auto-allowed exemptions: a single staged file, docs-only, or a"
    echo "  meta-edit to .cursor/rules/parallel-agents.mdc."
} >&2
exit 1
