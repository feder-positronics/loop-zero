---
name: implement
description: Implement a bounded change through behavior proof and the contract's owner queue route and merge authority.
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

Implement that bounded acceptance with behavior proof; use
[write-tests](../write-tests/SKILL.md) for test design and regression evidence.
For migrations,
authorization, locking/concurrency or policy logic, list edge cases in Acceptance
and map each to a test or recorded evidence. Observe required UI/runtime behavior within actual permissions; include visual
proof for UI changes per [Reporting](../../CONTRACT.md#reporting). Required
acceptance that remains unobserved stays open
with a next-action owner. Record the tested revision, observations, trade-offs
and limits in Notes or human Validation. Follow the contract's
[gate receipt fields and reuse rules](../../CONTRACT.md#gate-execution-and-delivery-priority).

Run commands expected to take more than about two minutes detached as in
[Long-running jobs](../../../docs/DELEGATION.md#long-running-jobs).
For builds, tests, CI, review, ready and merge, Codex exec/write_stdin yields
>=120 s (>=30 s otherwise); Claude uses a background completion notification
or `t3_background_wait`. Read the completed result once; never use
`t3_thread_read` or `task_status` for liveness. Cap returned output at the
producer: `gh api --jq` selected fields, shell `| head -c 2000`, thread reads
limit <=2, maxCharsPerItem <=600 and runLimit 1. Do not end the turn, settle or
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
or scheduling wakeups. The job must confirm a pushed branch, merged or closed PR,
retention of any evidence required by the delivery contract, clean tracked/untracked
Git state, an empty shared stash, inactivity, no unarchived T3 thread binding
where T3 is used, and its host age/size policy with a 24-hour mtime/ctime idle
floor. Honor host execution approval rules; use dry run until execution is
authorized. If no job is named or any condition is unproved, preserve the leftovers
and report cleanup to the owner. Successful `loopzero merge` already performs its
own cleanup under the delivery contract.

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget) for each pushed head. Check the
clean committed head before pushing. Suggestions must not expand scope. Follow
the contract's owner queue route and merge authority.
Rebase your own commits onto origin/main before the primary review.
Leave base merges out of a branch awaiting its delta.
After an approved primary or a delta, merge main into the existing branch when the base or its check scripts change.
Preserve the primary for the delta and the delta for
post-review fixes. Split the PR if resolving conflicts requires rewriting its reviewed history.

Do not bypass the sandbox, change the check list or enable `[checks].network`
to pass checks. Report base failures rather than work around them. If you must
work around a deterministic defect outside your scope, fix it in a follow-up PR
when small and test-only; otherwise file it labelled `urgent`, naming the PR it
blocks. Only you
commit, never checks, hooks or scripts. If acceptance requires unlisted paths,
update task and PR first; request scope decisions for expanded proof seams or
ownership. If interrupted, preserve an adoptable diff and report remaining
acceptance, checks and findings under the contract's handoff rules.

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
