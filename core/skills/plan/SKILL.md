---
name: plan
description: Plan outcome-sized tasks with observable acceptance, ownership, paths and checks. File authorized slices from active goals or blueprints.
---

# Plan

Read [the contract](../../CONTRACT.md), repository guidance, the issue/task and
relevant code. Use [delegation guidance](../../../docs/DELEGATION.md) for executors.

Restate the ask in one precise sentence and check it against the issue or request. Size work to one
independently acceptable outcome or failure family in one PR by default. Keep tests, generated files, docs and related
cleanup with their change to amortize checks, review and queueing. Batch non-urgent queued chores in
the same area and risk class, away from risky changes and behind urgent fixes. Split only at a named seam:
separate rollout or rollback, a design-changing measurement, higher risk (such as destructive migration
or a trust boundary), separate ownership or a diff too wide for reliable review.
Match planning depth to the cost of reversal. For persisted state, CLI/config interfaces, trust
boundaries, public APIs and shared abstractions, identify state and callers that must survive changes
to assumptions after merge. Use compact state tables for material transitions and settle cheap,
reversible choices during implementation. Make cheap design- or target-changing observations on real
data or environments first and derive targets from them. Record each task's objective, observable
acceptance, paths, owners, constraints and proving `workflow.toml` checks. Add checks only where existing
checks cannot cover the behavior.

When authorized to file slices from an active goal or blueprint, use its
tracking issue as the parent. Create each issue per
[BACKLOG](../../../docs/BACKLOG.md#for-agents). Name the parent step and the evidence it
needs in an `Unblocks: #<parent> (step N) — <evidence>` line. Link each child with
`gh api "repos/$repo/issues/$parent/sub_issues" -F sub_issue_id="$id"`. Use `owner/repo`, parent issue number and the child's numeric REST `id`, respectively.

Record the plan in `.loopzero/task.md` using [HANDOFF](../../HANDOFF.md), without
another tracking file. Resolve routine choices from evidence; missing authority,
conflicting ownership or material requirements need the exact decision and options.
Continue independent preparation. Do not implement or plan unrelated cleanup.

Then continue the task you were given as far as it authorizes, or hand over
what is left of its acceptance.
