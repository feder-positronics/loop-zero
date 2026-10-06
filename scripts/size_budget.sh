#!/usr/bin/env bash
# Size budget shared by CI and `loopzero check`. Caps are absolute. A candidate
# over a cap fails only if it also grew against its base, so a repository that
# is already over can still merge changes that hold or shrink and never deadlocks.
# Usage: scripts/size_budget.sh [BASE]   (default: merge-base with origin/main)
set -euo pipefail
# Raised for REST quota repair and regression coverage (owner approval 2026-09-24).
# Raised to restore readability after #259/#260 compaction (owner approval 2026-10-06).
SRC_CAP=4800
TESTS_CAP=6400
base=${1:-$(git merge-base HEAD origin/main)}
count_base() {
  git -c core.quotepath=off ls-tree -rz --name-only "$base" -- "$1" | { grep -z '\.py$' || true; } \
    | xargs -0 -r -I{} git show "$base:{}" | wc -l
}
# The candidate is the working tree, so uncommitted and new files count too.
count_tree() {
  git -c core.quotepath=off ls-files -zco --exclude-standard -- "$1" | { grep -z '\.py$' || true; } \
    | xargs -0 -r sh -ec 'for f; do if [ -f "$f" ]; then cat -- "$f"; fi; done' _ | wc -l
}
status=0
report() { # name dir cap
  local now was
  now=$(count_tree "$2"); was=$(count_base "$2")
  echo "$1=$now (base $was, delta $((now - was)), cap $3, headroom $(($3 - now)))"
  if [ "$now" -gt "$3" ] && [ "$now" -gt "$was" ]; then status=1; fi
}
report src src/loopzero "$SRC_CAP"
report tests tests "$TESTS_CAP"
[ "$status" -eq 0 ] || echo "size budget: FAIL (over cap and grew against base)"
exit "$status"
