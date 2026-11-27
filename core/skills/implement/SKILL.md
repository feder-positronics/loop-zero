---
name: implement
description: Deliver a bounded task or PR slice with behavior proof. Use work-issue for full GitHub issue delivery.
---

# Implement

Read [the contract](../../CONTRACT.md), `.loopzero/task.md`, repository guidance
and affected interfaces. Confirm the task worktree and branch. Keep
[keys and login tokens out of output](../../CREDENTIALS.md#keep-credentials-out-of-output).
Follow the full [credential conventions](../../CREDENTIALS.md) before API-key
integration and [delegation guidance](../../../docs/DELEGATION.md) before
launching subagents or commissioning delegates.

As an unattended worker, follow [the worker role's blocker route](../../../docs/DELEGATION.md#team-roles).

Before the first code edit, record observable criteria in task Acceptance and
freeze them. Change Acceptance only with a recorded reason in Notes, before
implementing the change. For an authorized retarget, make `.loopzero/task.md`
and the PR description agree on Acceptance before the next review.
For changes touching guards, auth, permissions,
sessions, secrets or trust boundaries, record a short design pass in Notes
before the first code edit: threats, invariants and chosen approach.
When deduplicating tests, list their distinct assertions before editing,
keep each in a surviving test, and record where in the PR body.

Use [write-tests](../write-tests/SKILL.md) to prove the bounded acceptance.
Map migration, authorization, locking/concurrency and policy edge cases to tests
or evidence in Acceptance. Observe required UI and runtime behavior under actual
permissions, with visual proof for UI changes per [Reporting](../../CONTRACT.md#reporting).
Leave unobserved acceptance open with a next-action owner. Record revision,
observations, trade-offs and limits in Notes or human Validation.
Focused or ongoing checks are intermediate. Follow the [receipt reuse rules](../../CONTRACT.md#check-reuse-and-delivery-priority).

Run commands expected to exceed about two minutes as [long-running jobs](../../../docs/DELEGATION.md#long-running-jobs)
and follow the brief's [waiting and output rules](../../../docs/DELEGATION.md#briefs-and-ownership).
Do not report done before the job's `LOG.exit` exists, unless you report it killed and failed.

Keep receipts, evidence, review artifacts and handoffs on the PR, issue or named durable scratch;
reserve `/tmp` for running-command logs, builds and browser runs. At task finish, leave leftover
worktrees and generated files to the [retention job](../../../docs/DELEGATION.md#leftover-cleanup) named in host
or repository instructions; with no named job or unverified conditions, preserve them and report cleanup to the owner.
For confirmed-abandoned tasks outside its scope, give the owner the [manual procedure](../../../SETUP.md#removing-an-abandoned-task-worktree).

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget) for each pushed head, including base updates after review.
Keep review suggestions within scope. Finish planned test, documentation and
generated-file edits before primary review.
Before the check and draft push for primary review, rebase your own commits onto `origin/<base>` using
`[repo].base` (default `main`); open the draft before a long check. For a stack, follow
[stacked delivery](../../../docs/DELEGATION.md#stacked-delivery).
Leave base merges out of a branch awaiting its delta unless it needs a conflict refresh;
then merge the base before checking and reviewing the resolved head.

Never bypass the sandbox, change the check list or enable `[checks].network` to pass checks.
Report base failures without working around them.
For a deterministic out-of-scope defect requiring a workaround, use a follow-up PR for a small
test-only fix or file an `urgent` issue naming the blocked PR. Commit yourself, outside checks, hooks
and scripts. Update task and PR before adding paths required by acceptance. Obtain scope decisions
before expanding proof seams or ownership. On interruption, preserve an adoptable diff and use the
contract to report outstanding acceptance, checks and findings.

Keep host and runner changes to ports, env, paths, services or labels compatible with current main,
or wait for the consuming change to land. Prove the affected job on the changed host with one green
run before enabling it for all runners. Before parking a PR, verify it does not wait on yours. Never park both on each other.
Immediately hand failures outside the contract's owner queue route and their last-green comparison
to [diagnose](../diagnose/SKILL.md). A log entry alone does not hand over the failure.
