---
name: implement
description: Implement a bounded change through behavior proof and the contract's S1/S2 owner rules.
---

# Implement

Read [the contract](../../CONTRACT.md), `.loopzero/task.md`, repository guidance
and affected interfaces. Confirm the task worktree and branch. Follow
[credential conventions](../../CREDENTIALS.md) and
[delegation guidance](../../../docs/DELEGATION.md).

As an unattended worker, never use an ask-the-user or request-input tool. Send
one message with a blocker or question to the parent (or owner named in the
brief), then end the turn.

Before the first code edit, record observable criteria in task Acceptance and
freeze them. Change Acceptance only with a recorded reason in Notes, before
implementing the change. For changes touching guards, auth, permissions,
sessions, secrets or trust boundaries, record a short design pass in Notes
before the first code edit: threats, invariants and chosen approach.

Implement that bounded acceptance with behavior proof; use
[write-tests](../write-tests/SKILL.md) for test design. For a bug fix, watch the
regression test fail on pre-fix code and record where in Notes; a test that passes
without the fix proves nothing. For migrations,
authorization, locking/concurrency or policy logic, list edge cases in Acceptance
and map each to a test or recorded evidence. Observe required UI/runtime behavior
within actual permissions. Required acceptance that remains unobserved stays open
with a next-action owner. Record tested revisions, observations, commands,
results, trade-offs and limits in Notes or human Validation; evidence from an
earlier head cannot establish a current gate verdict.
Apply [local gate receipt reuse](../../CONTRACT.md#blockers).

Run a command expected to take more than about two minutes (builds, browser
runs, full checks) detached, so a harness that ends your turn cannot kill it:
`setsid nohup bash -c 'echo $$ >LOG.pid; <cmd> >LOG 2>&1; echo $? >LOG.exit' </dev/null >/dev/null 2>&1 &`,
then block on `until test -f LOG.exit; do sleep 30; done`.
For builds, tests, CI, review, ready and merge, Codex exec/write_stdin yields
>=120 s (>=30 s otherwise); Claude uses a background completion notification
or `t3_background_wait`. Read the completed result once; never use
`t3_thread_read` or `task_status` for liveness. Cap returned output at the
producer: `gh api --jq` selected fields, shell `| head -c 2000`, thread reads
limit <=2, maxCharsPerItem <=600 and runLimit 1. Do not end the turn, settle or report done while a job
you started has no `LOG.exit`; if the turn must end, first schedule a one-shot
wakeup (T3 `schedule:{type:once,runAt:<ISO time>}`) that reads `LOG.exit` and resumes the delivery step it feeds. To cancel, check `LOG.exit` first, then run
`kill -- -$(cat LOG.pid)`; never `pkill -f`, which also matches your own shell
(`$!` is not the job: `setsid` may fork). Codex 0.165 sends SIGTERM (exit 143) to such
children (openai/codex#46702); drop this once a fixed release is verified.

Treat /tmp as disposable: only running-command logs, build output and browser
runs belong there. Keep receipts, evidence, review artifacts and handoffs on the
PR or issue, or in the durable scratch your repository's instructions name. Make
task checkouts as worktrees of an existing clone, not new clones. At task finish,
hand leftover worktrees and generated files to the periodic retention job named
in host or repository instructions (such as `scratch-retention`), without waiting
or scheduling wakeups. The job must confirm a pushed branch, merged or closed PR,
verified archive handoff of required review evidence, clean tracked/untracked
Git state, an empty shared stash, inactivity, no unarchived T3 thread binding
where T3 is used, and its host age/size policy with a 24-hour mtime/ctime idle
floor. Honor host execution approval rules; use dry run until execution is
authorized. If no job is named or any condition is unproved, preserve the leftovers
and report cleanup to the owner. Successful `loopzero merge` already performs its
own cleanup under the delivery contract.

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget). Check the clean committed head
before pushing; publish and review every pushed head within that budget. Batch
all substantive and mechanical repairs into one delta head. Preserve an approved
head unless necessary changes require renewed checks and review. Suggestions
must not expand scope. Follow the contract's S1 owner route and S2 merge authority.
Never merge main into a reviewed branch. Rebase your own commits onto
origin/main BEFORE review. If a conflict appears AFTER review: rebase (drop any
merge), check, then one fresh whole-PR primary; that is the PR's one fresh lineage.

Do not bypass the sandbox, change the check list or enable `[checks].network`
to pass checks. Report base failures rather than work around them. Only you
commit, never checks, hooks or scripts. If acceptance requires unlisted paths,
update task and PR first; request scope decisions for expanded proof seams or
ownership. If interrupted, preserve an adoptable diff and report remaining
acceptance, checks and findings under the contract's handoff rules.

A host or runner change that alters what jobs see (ports, env vars, paths,
services, labels) goes live only after the repository change consuming it is on
main, or stays compatible with current main; prove it with one green run of the
affected job on the changed host before enabling it for every runner. Before
parking a PR behind another, confirm that one does not wait on yours; never hold
two PRs on each other. A failure outside the contract's S1 owner route goes with
its last-green comparison to [diagnose](../diagnose/SKILL.md) at
once; recording it is not handing it over.

To requeue, unless an S2 hold applies, post `@mergifyio queue <queue>` as the
whole PR comment from the same login; `loopzero merge --wait` never requeues
by itself.
