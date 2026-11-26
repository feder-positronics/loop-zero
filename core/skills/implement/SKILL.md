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
implementing the change. For changes touching guards, auth, permissions,
sessions, secrets or trust boundaries, record a short design pass in Notes
before the first code edit: threats, invariants and chosen approach.

Use [write-tests](../write-tests/SKILL.md) to prove the bounded acceptance.
Map migration, authorization, locking/concurrency and policy edge cases to tests
or evidence in Acceptance. Observe required UI and runtime behavior under actual
permissions, with visual proof for UI changes per [Reporting](../../CONTRACT.md#reporting).
Leave unobserved acceptance open with a next-action owner. Record revision,
observations, trade-offs and limits in Notes or human Validation.
Primary review requires a completed passing `loopzero check` receipt at the pushed head.
Focused or ongoing checks are intermediate. Follow the [receipt reuse rules](../../CONTRACT.md#gate-execution-and-delivery-priority).

Follow [long-running jobs](../../../docs/DELEGATION.md#long-running-jobs) for commands
expected to exceed about two minutes, and the [wait/output](../../../docs/DELEGATION.md#briefs-and-ownership)
and [caps](../../../docs/DELEGATION.md#coordinator-rollover) rules. Set `runLimit 1` on thread reads.
Hold the turn open until your job has `LOG.exit` or the unit is gone and you report it killed and failed.
If you must yield, first schedule a one-shot T3 wakeup (`schedule:{type:once,runAt:<ISO time>}`)
to read `LOG.exit` and resume its delivery step.

Use worktrees of an existing clone. Keep receipts, evidence, review artifacts and handoffs on the PR,
issue or named durable scratch. Reserve `/tmp` for running-command logs, builds and browser runs.
Successful merge already runs contract cleanup. Hand leftovers to the named periodic retention job
(such as `scratch-retention`) without waiting or scheduling wakeups.
Follow [Leftover cleanup](../../../docs/DELEGATION.md#leftover-cleanup) when verifying or operating it.
With no named job or unproved conditions, preserve leftovers and report cleanup to the owner.
For confirmed-abandoned tasks outside its scope, give the owner the
[manual worktree procedure](../../../SETUP.md#removing-an-abandoned-task-worktree).

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget) for each pushed head.
Suggestions must not expand scope.
Before the check and draft push for primary review, rebase your own commits onto `origin/<base>` using
`[repo].base` (default `main`). For a stack, follow
[stacked delivery](../../../docs/DELEGATION.md#stacked-delivery).
Leave base merges out of a branch awaiting its delta.
After review, follow the [Review budget](../../CONTRACT.md#review-budget) for base or check-script
changes. Keep the primary as the delta's base and reserve the delta for post-review fixes.

Run checks with unchanged sandbox, check list and network settings. Report base failures as found.
For a deterministic out-of-scope defect requiring a workaround, use a follow-up PR for a small
test-only fix or file an `urgent` issue naming the blocked PR. Commit yourself, outside checks, hooks
and scripts. Update task and PR before adding paths required by acceptance. Obtain scope decisions
before expanding proof seams or ownership. On interruption, preserve an adoptable diff and use the
contract to report outstanding acceptance, checks and findings.

Keep host and runner changes to ports, env, paths, services or labels compatible with current main,
or wait for the consuming change to land. Prove the affected job on the changed host with one green
run before enabling it for all runners. Check for mutual dependencies before parking a PR.
Immediately hand failures outside the contract's owner queue route and their last-green comparison
to [diagnose](../diagnose/SKILL.md).

To requeue without a merge-authority hold, post `@mergifyio queue` as the whole PR comment from the
same login. Requeueing requires that comment even after `loopzero merge --wait`.
