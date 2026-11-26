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

Run commands expected to take more than about two minutes detached as in
[Long-running jobs](../../../docs/DELEGATION.md#long-running-jobs).
Follow [wait and output](../../../docs/DELEGATION.md#briefs-and-ownership)
and [caps](../../../docs/DELEGATION.md#coordinator-rollover). Set
`runLimit 1` for thread reads. Do not end the turn, settle or
report done while a job you started has no `LOG.exit`, unless the unit is gone
and you report the job as killed and the result as failed. If the turn must
end, first schedule a one-shot wakeup (T3 `schedule:{type:once,runAt:<ISO time>}`)
that reads `LOG.exit` and resumes the delivery step it feeds.

Treat /tmp as disposable: only running-command logs, build output and browser
runs belong there. Keep receipts, evidence, review artifacts and handoffs on the
PR or issue, or in the durable scratch your repository's instructions name. Make
task checkouts as worktrees of an existing clone, not new clones. At task finish,
hand leftover worktrees and generated files to the periodic retention job named
in host or repository instructions (such as `scratch-retention`), without waiting
or scheduling wakeups. When verifying or operating that job, follow
[Leftover cleanup](../../../docs/DELEGATION.md#leftover-cleanup).
If no job is named or any condition is unproved, preserve the leftovers
and report cleanup to the owner. Successful `loopzero merge` already performs its
own cleanup under the delivery contract. For a confirmed-abandoned task outside
the job's scope, give the owner the
[manual worktree procedure](../../../SETUP.md#removing-an-abandoned-task-worktree).

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget) for each pushed head.
Suggestions must not expand scope.
Before the check and draft push for primary review, rebase your own commits onto `origin/<base>` using
`[repo].base` (default `main`). For a stack, follow
[stacked delivery](../../../docs/DELEGATION.md#stacked-delivery).
Leave base merges out of a branch awaiting its delta.
For base or check-script changes after review, follow the
[Review budget](../../CONTRACT.md#review-budget).
Preserve the primary for the delta and the delta for
post-review fixes. Split the PR if resolving conflicts requires rewriting its reviewed history.

Never bypass the sandbox, change the check list or enable `[checks].network` to pass checks.
Report base failures without working around them. If a deterministic out-of-scope
defect requires a workaround, use a follow-up PR for a small test-only fix or file an `urgent` issue naming the
blocked PR. Commit yourself, never from checks, hooks or scripts. Update task and
PR before adding paths required by acceptance, and get scope decisions before
expanding proof seams or ownership. On interruption, preserve an adoptable diff
and report remaining acceptance, checks and findings per the contract.

A host or runner change that alters what jobs see (ports, env vars, paths,
services, labels) goes live only after the repository change consuming it is on
main, or stays compatible with current main; prove it with one green run of the
affected job on the changed host before enabling it for every runner. Before
parking a PR behind another, confirm that one does not wait on yours; never hold
two PRs on each other. A failure outside the contract's owner queue route goes
with its last-green comparison to [diagnose](../diagnose/SKILL.md) at
once; recording it is not handing it over.

To requeue, unless a merge-authority hold applies, post `@mergifyio queue` as the
whole PR comment from the same login; `loopzero merge --wait` never requeues
by itself.
