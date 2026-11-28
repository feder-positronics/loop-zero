---
name: plan
description: Plan outcome-sized tasks with observable acceptance, ownership, paths and checks. File authorized slices from active goals or blueprints.
---

# Plan

Read [the contract](../../CONTRACT.md), repository guidance, the issue/task and
relevant code. Use [delegation guidance](../../../docs/DELEGATION.md) for executors.

Restate and verify the ask in one precise sentence. Default to one independently
acceptable outcome or failure family per PR. Include related tests, generated
files, docs and cleanup to share checks, review and queueing. Batch non-urgent
queued chores by area and risk, separate from risky work and behind urgent fixes.
Split at a named seam: rollout/rollback, design-changing measurement, higher
risk (e.g. destructive migration or trust boundary), ownership or reviewability.

Match depth to reversal cost. For persisted state, CLI/config interfaces,
trust boundaries, public APIs and shared abstractions, name the state and callers
that must survive changed assumptions after merge. Use compact state tables for
material transitions. Settle cheap reversible choices during implementation.
Take cheap observations on real data or environments that can change design or
targets first, and derive targets from them. Record objective, observable
acceptance, paths, owners, constraints and proving `workflow.toml` checks.
Add checks only when existing ones cannot cover behavior.

When authorized to file slices from an active goal or blueprint, use its
tracking issue as the parent. Create each issue per
[BACKLOG](../../../docs/BACKLOG.md#for-agents). Name the parent step and the evidence it
needs in an `Unblocks: #<parent> (step N) — <evidence>` line. Link each child with
`gh api "repos/$repo/issues/$parent/sub_issues" -F sub_issue_id="$id"`. Use `owner/repo`, parent issue number and the child's numeric REST `id`, respectively.

Use [HANDOFF](../../HANDOFF.md) in `.loopzero/task.md` as the sole plan file.
Resolve routine choices from evidence. For missing authority, conflicting
ownership or material requirements, name the decision and options. Continue
independent preparation and authorized work, then hand off remaining acceptance.
Exclude unrelated cleanup from planning and implementation.
