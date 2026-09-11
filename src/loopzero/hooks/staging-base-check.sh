#!/usr/bin/env bash
# Refuse to commit on staging / main when the local branch is behind origin.
#
# Why: an agent or operator who runs `git commit` directly on a stale
# local staging produces a divergent commit (parallel branch off the
# pre-pull tip). When pushed, the divergent commit either:
#   - drops collateral content from a parallel merge (silent regression), or
#   - forces a rebase + force-push that risks losing work.
# This guards against the first failure mode at commit time.
#
# Reads local refs only; no `git fetch`, so no network latency added to
# commits. Catches the common case where the user has already fetched
# origin (e.g. just ran `gh pr merge`) but forgot to `git pull` before
# the next commit.
#
# Bypass: set STAGING_BASE_BYPASS=1 in the environment.

set -e

current="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
protected=0
for branch in ${LOOPZERO_PROTECTED_BRANCHES:-staging main}; do
  [ "$current" != "$branch" ] || protected=1
done
[ "$protected" -eq 1 ] || exit 0

# Compare local HEAD against the last-known origin tip. No fetch.
if ! git rev-parse --verify --quiet "origin/$current" >/dev/null; then
  # No tracking ref yet — nothing to compare. Allow.
  exit 0
fi

behind="$(git rev-list --count "HEAD..origin/$current" 2>/dev/null || echo 0)"

if [ "$behind" -eq 0 ]; then
  exit 0
fi

if [ -n "${STAGING_BASE_BYPASS:-}" ]; then
  echo "⚠️  $current is $behind commit(s) behind origin/$current — bypassing (STAGING_BASE_BYPASS=1)." >&2
  exit 0
fi

cat >&2 <<EOF
❌ Refusing to commit on $current: local is $behind commit(s) behind origin/$current.

   Committing directly on a stale base creates a divergent local branch
   that will diverge from origin and may drop collateral content from a
   parallel merge.

   Resolve before continuing:
     git stash -u           # if you have uncommitted WIP
     git pull --rebase
     git stash pop          # if you stashed

   To bypass intentionally (e.g. recovering a known-good divergent state):
     STAGING_BASE_BYPASS=1 git commit ...

   See AGENTS.md § Worktree per task for why direct commits on staging/main
   are discouraged in agent sessions.
EOF
exit 1
