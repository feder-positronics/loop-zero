---
name: implement
description: Deliver a bounded task or PR slice with behavior proof. Use work-issue for full GitHub issue delivery.
---

# Implement

Read [the contract](../../CONTRACT.md), `.loopzero/task.md`, repository guidance
and affected interfaces. Confirm the task worktree and branch. Keep
[keys and login tokens out of output](../../CREDENTIALS.md#keep-credentials-out-of-output).
Follow the full [credential conventions](../../CREDENTIALS.md) before API-key
integration and [model selection](../../../docs/DELEGATION.md#resolve-the-model-before-delegation)
and [briefs](../../../docs/DELEGATION.md#briefs-and-ownership) before delegating.
Unattended workers follow the [blocker route](../../../docs/DELEGATION.md#worker).

Before the first code edit, record observable criteria in task Acceptance and
freeze them. Change Acceptance only with a recorded reason in Notes, before
implementing the change. For an authorized retarget, make `.loopzero/task.md`
and the PR description agree on Acceptance before the next review.
For changes touching guards, auth, permissions,
sessions, secrets or trust boundaries, record a short design pass in Notes
before the first code edit: threats, invariants and chosen approach.
When deduplicating tests, list their distinct assertions before editing,
keep each in a surviving test, and record where in the PR body.

Use [write-tests](../write-tests/SKILL.md) when adding or changing tests.
Map migration, authorization, locking/concurrency and policy edge cases to tests
or evidence in Acceptance. Observe required UI and runtime behavior under actual
permissions, with visual proof for UI changes per [Reporting](../../CONTRACT.md#reporting).
Leave unobserved acceptance open with a next-action owner. Record revision,
observations, trade-offs and limits in Notes or human Validation.
Follow the [receipt reuse rules](../../CONTRACT.md#check-reuse-and-delivery-priority).

Run commands expected to exceed about two minutes as [long-running jobs](../../../docs/DELEGATION.md#long-running-jobs)
and follow the brief's waiting and output limits.

Keep receipts, evidence, review artifacts and handoffs on the PR, issue or named durable scratch;
reserve `/tmp` for running-command logs, builds and browser runs. Use `loopzero start`
from an existing clone for new task worktrees and keep its task-file ownership marker.
After verified merge or PR closure, run the contract's merge command to finish cleanup.
At task finish, leave leftover
worktrees and generated files to the [retention job](../../../docs/DELEGATION.md#leftover-cleanup) named in host
or repository instructions; with no named job or unverified conditions, preserve them and report cleanup to the owner.
For confirmed-abandoned tasks outside its scope, give the owner the [manual procedure](../../../SETUP.md#removing-an-abandoned-task-worktree).

Finish planned tests, docs and generated files before primary review.
Apply the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget) to every pushed head.
With a passing local receipt and an eligible review, start `loopzero ready --wait`
even if hosted checks are pending or missing. When it exits 0, start
`loopzero merge --wait`. Let the active waiter follow hosted checks and handle
any refusal through the contract's [blocker rules](../../CONTRACT.md#blockers).
Keep review suggestions within scope.
Before the first draft push and check, rebase onto `origin/<base>` (`[repo].base`,
default `main`). Keep unrelated base updates out of a pending delta.
For a stack, follow [stacked delivery](../../../docs/DELEGATION.md#stacked-delivery).

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
Before requeueing, reproduce the failures named in the dequeue report at the new head. If they also fail on main, follow the [main-failure route](../../CONTRACT.md#blockers).
