#!/usr/bin/env bash
# Line budget shared by CI and `loopzero check`. Caps are absolute. A budget over
# its cap fails only if it also grew against its base, so a repository that is
# already over can still merge changes that hold or shrink and never deadlocks.
# Function complexity is Ruff's C901 rule (pyproject.toml).
# Usage: scripts/size_budget.sh [BASE]   (default: merge-base with origin/main)
set -euo pipefail
# Each run prints current totals and headroom against these caps.
SRC_CAP=8250
TESTS_CAP=13850
CORE_DOCS_CAP=1500
DELEGATION_DOC_CAP=660
base=${1:-$(git merge-base HEAD origin/main)}
count_base() {
  local path=$1 ext=$2
  git -c core.quotepath=off ls-tree -rz --name-only "$base" -- "$path" | { grep -z "$ext" || true; } \
    | xargs -0 -r -I{} git show "$base:{}" | wc -l
}
# The candidate is the working tree, so uncommitted and new files count too.
count_tree() {
  local path=$1 ext=$2
  git -c core.quotepath=off ls-files -zco --exclude-standard -- "$path" | { grep -z "$ext" || true; } \
    | xargs -0 -r sh -ec 'for f; do if [ -f "$f" ]; then cat -- "$f"; fi; done' _ | wc -l
}
status=0
report() { # name path cap ext_re
  local now was
  now=$(count_tree "$2" "$4"); was=$(count_base "$2" "$4")
  echo "$1=$now (base $was, delta $((now - was)), cap $3, headroom $(($3 - now)))"
  if [ "$now" -gt "$3" ] && [ "$now" -gt "$was" ]; then status=1; fi
}
report src src/loopzero "$SRC_CAP" '\.py$'
report tests tests "$TESTS_CAP" '\.py$'
report core_docs core "$CORE_DOCS_CAP" '\.md$'
report delegation_doc docs/DELEGATION.md "$DELEGATION_DOC_CAP" '\.md$'

[ "$status" -eq 0 ] || echo "size budget: FAIL (over cap and grew against base)"
exit "$status"
