---
name: implement
description: Deliver an understood bounded change with its owning behavior proof and repository-required checks.
---

Read [the shared contract](../../CONTRACT.md), local acceptance and relevant
selected profiles. Make the smallest complete change under the repository's
testing contract. Run the owning checks and record actual commands/results;
broaden validation when the changed behavior or local policy warrants it.

Stop on scope or ownership conflict. Run all applicable deterministic gates
before freezing the tree for one independent PR review. Validation children,
including commit hooks, must meet the contract's environment and Git-metadata
containment rules; only the parent commits adopted changes.

Resolve critical/important findings or obtain an explicit PR waiver from merge
authority. Rerun gates after repairs; mechanical lint/format/ratchet repairs with
unchanged behavior need no re-review. Substantive repairs use at most one bounded
delta review. Return to editing without a phase lock and preserve an adoptable
diff on timeout. Update the one PR evidence block for the exact publication head.
Consciously curate known gaps at closeout without copying suggestions or a
finding backlog. Finish with the [handoff](../../HANDOFF.md), then stop at READY
unless separately authorized to merge.

Apply the contract's CHECK CLASSES: only required diff-scoped deterministic
class-1 checks block on a code verdict (lint, types, tests, ratchets, schema/
contract checks, changed-file links). Class-2 repository health (full external
links, dependency audits, cost/usage alerts, whole-tree docs governance) runs on
main's schedule and updates one tracking issue, never a PR gate. Class-3 runner
loss, cancelled concurrency or network timeout gets one automatic retry; report
a second failure as infrastructure, not code. Missing required evidence is not
a pass. Check `[checks]` and branch protection for alignment.

For an unrelated class-1 failure, enforce every OVERRIDE condition in the shared
contract: identical failure on the exact current base with matched inputs and
linked logs, exact candidate, tracking issue, non-risk prose path cap and risk
exclusions, independent review and merge-authority approval, append-only
`CHECK-OVERRIDES.md` entry, matching merge commit trailer, and weekly maintainer
review. Only the prescribed audit append may follow the evidenced candidate;
other changes invalidate evidence. The read-only checks report grants no waiver.
