# Task handoff

Keep one authoritative evidence block in the PR body; before publication use
the existing task body, then move the block to the PR and leave only its link.
Update that block on head changes rather than making a second ledger or report.

- Objective and acceptance criteria.
- Repository, branch, base and exact current head; dirty/untracked changes.
- Core source revision and selected profiles from `workflow.toml`.
- Current owner/session, allowed paths and shared-resource reservations.
- Completed changes, deterministic gate commands, directories and pass/fail
  results at this head; inapplicable gates with reasons and missing gates as blockers.
- Independent reviewer, exact reviewed commit and result; at most one bounded
  delta, or mechanical-change classification with gates rerun at the new head.
- Critical/important findings resolved or explicitly waived by merge authority
  in this PR, with rationale; unresolved blocking findings and review limits.
- Publication head, remaining merge conditions and next action with its owner.
- Closeout decision on the single capped `KNOWN-GAPS.md`; no automatic carryover
  of PR findings or suggestions, and no durable finding counts.

Transfer a frozen candidate explicitly between runtimes. The reviewer gets the
exact patch and acceptance evidence; the previous writer stays stopped until
ownership returns. A timeout leaves an adoptable diff, including untracked files,
with the next action recorded. Supersede stale READY evidence when the head
changes, retaining the reviewed commit only as provenance in the same block.
READY requires complete checks and review coverage for the current head; it
does not mean merged. Stop at READY unless merge is explicitly authorized.

## Adopting generation-owned review authority from loop-zero 0.3.2

Adoption is a projection of existing evidence, not a replay of historical
work. With dispatch writers stopped, preserve the complete authority ledger and
consumer trust receipts, install and pin the new loop-zero release, and let the
new package authenticate that history before the first new admission. Do not
rewrite old task payloads, task hashes, terminals, verdicts, or receipts.

An authenticated legacy whole-manifest pass remains the seed at its original
source identity, tree, and manifest digest. An unchanged source therefore
reuses that pass without verification or review. If the source later changes,
the first claim-level trust run is conservatively repository-wide; subsequent
runs can carry unaffected per-claim verdicts. Existing delivery verdicts are
projected into content generations and carry only across package-proven
equivalent heads, so upgrade itself creates neither a launch nor a new slot.

Start only new-release writers after the projection succeeds. Retain the
downgrade barrier and authority archives: an older binary must not write once
new authority record families exist. Historical observational rows remain
visible to `loopzero review-stats`, with fields unavailable in 0.3.2 reported as
`unrecorded` or `unknown`, never fabricated as labels or zero cost.
