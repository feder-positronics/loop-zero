# Work Issue Delivery Safeguards

Read each section when its condition applies.

## Implementation boundaries

- API shape changes require regenerated checked-in schema/client artifacts and
  caller validation against them.
- Seeded browser state or changed prerequisites require fresh-stack proof,
  not just an already-running stack.
- Before review, record documentation impact; update docs or give an
  evidence-backed reason they need no update.
- Before publication, inspect the issue and linked artifacts for its lifecycle
  blueprint. Include required completion in the closing delivery. A child issue
  does not own its parent's blueprint.

## Evidence for approval

Counts, censuses and states presented for decisions carry provenance on the
same line: environment, service/host, connection resolution, read time. Never
label local/staging data production. Restate provenance when requesting
production-change approval. If the source proves wrong, disclose it, void the
approval and ask again.

## Inherited work

Before continuing an existing branch or PR, identify which acceptance remains
unmet. Inspect relevant base changes since divergence and competing deliveries
for that acceptance slice. On the owning issue or PR, recommend **continue**,
**rework**, or **propose closure**, with the evidence; obsolete work need not be
rebased first. Closure, history rewriting and replacement PRs still need their
applicable authorization. Continue in place by default, preserving original
commits and contributor credit under the [handoff rules](../../../docs/DELEGATION.md#coordinator-rollover).
When authorized integration rewrites commits, use `git range-diff` to inspect
what changed, then obtain behavior proof, checks and review for the resulting
head under the existing rules. A series comparison is not behavior proof.

## Integration, waiting and re-entry

- Without a base merge queue, defer a merely-behind branch's rebase during
  implementation/repairs. Rebase once when otherwise finished, then check and
  request exact-head review; an earlier review is wasted. Update the base by
  rebase, never a base merge: `loopzero review` refuses a delta across a merge
  commit. Serialize parallel tasks' final integration.
- Treat the issue as singly owned; perform the checks before editing,
  publication and merge in [Work Issue](SKILL.md). Stop and reconcile
  competing deliveries.
- Review, CI and observable post-merge acceptance waits remain this task;
  neither bypass them nor start another issue.
- For CI or observation-based acceptance, record the behavior exercised,
  lane, landed head and interval or sample count. A green run that skipped the
  required behavior is not closure evidence.
- After interruption/context loss, re-read the issue, PR head, remote branch,
  CI and open threads before resuming. Follow the
  [contract's resume/wait rules](../../CONTRACT.md#the-six-steps), not memory.

## Late evidence

- If partial delivery auto-closes the issue, inspect the event and delivery
  evidence. If a closing reference caused it while acceptance remains, remove
  editable PR references, exclude them from subsequent commits, reopen under
  existing task authorization and verify open state. Do not rewrite merged
  history solely to remove a reference.
- After completed acceptance, late findings need a fresh follow-up branch/PR.
  Reopen the issue only for regression of previously satisfied acceptance.
- Ordinary monitoring after shipped acceptance is separate work. Later evidence
  needs a newly requested review/issue; create no reminder issues.
- Deferred work requires a separate request, preferably a fresh session.
