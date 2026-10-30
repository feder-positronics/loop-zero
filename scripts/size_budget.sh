#!/usr/bin/env bash
# Size budget shared by CI and `loopzero check`. Caps are absolute. A candidate
# over a cap fails only if it also grew against its base, so a repository that
# is already over can still merge changes that hold or shrink and never deadlocks.
# Usage: scripts/size_budget.sh [BASE]   (default: merge-base with origin/main)
set -euo pipefail
# Raised for REST quota repair and regression coverage (owner approval 2026-09-24).
# Raised to restore readability after #259/#260 compaction (owner approval 2026-10-06).
# Recovery preserves exact publication identity, a shared read deadline and boundary
# proofs (parent-authorized 2026-10-07); no checks or review gates are removed.
# Completed-task publication guard adds typed bounded REST history and owning
# refusal/compatibility proofs (parent-authorized 2026-10-07, measured 4873/6611).
# Ready-wait post-transition revalidation and public CLI boundary proofs
# (parent-authorized 2026-10-07, measured 4884/6753); all check commands remain.
# Review-only runtime authority and complete mount-boundary proofs
# (owner approval 2026-10-07, caps 4950/6950); all check commands remain.
# Remaining R2-R4 accepted improvements, owning proofs and formal-review repairs
# (owner approval 2026-10-07, caps 5250/7600); all check commands remain.
# OD approval 2026-10-27: structured startup validation, closed-five adjudication, local/hosted eligibility and owning regressions.
# OD approval 2026-10-27: complete Codex raw transport, strict public projection and preserved boundary proofs.
# OD ruling 125(a), ruling 123 Note 1: measured source overage +7 (6000 -> 6007).
# OD40 2026-10-28: reviewed full native pair, private failed-output retention and shared consumers; exact owning coverage, zero headroom.
# OD48 2026-10-29: main-only integration growth +75 source / +134 tests;
# PR-owned deltas unchanged (+539/+1298); exact 6546/10644, zero headroom.
# OD48 ruling 2026-10-29 (#321): metadata-only stage liveness is a tiny CLI change;
# exact own +35 source / +93 tests on original 776bc1f6 (6007/9346).
# Main 917a3237 adds +539/+1298 to that base (6546/10644); only main-growth
# shifts are authorized: integrated caps 6581/10737, zero headroom.
# OD v58 2026-10-30: Codex 0.165 native rollout provenance (banner-less --json), fail-closed; exact own +34 source / +70 tests, zero headroom.
# OD v58 2026-10-30: fatal-startup lineage markers and projection parity; exact own +1 source / +11 tests, zero headroom.
SRC_CAP=6616
# Managed-account and native quota regression coverage (owner approval 2026-10-22).
# OD approval 2026-10-27: complete recovery repost and raw UTF-8 leading-block boundary proofs.
# OD ruling 123 Note 1: exact measured test overage (+61); source remains below its cap.
# OD ruling 125(a), ruling 123 Note 1: measured test overage +35 (9311 -> 9346).
# Marcin approval 2026-10-29: ai-accounts reserve guard, per-window resets and fallback and caller-credential proofs (+85).
TESTS_CAP=10903
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
