---
name: implement
description: Implement a bounded change through behavior proof and readiness; merge only when separately authorized.
---

# Implement

Read [the contract](../../CONTRACT.md), `.loopzero/task.md`, repository guidance
and affected interfaces. Confirm the task worktree and branch. Follow
[credential conventions](../../CREDENTIALS.md) and
[delegation guidance](../../../docs/DELEGATION.md).

As an unattended worker, never use an ask-the-user or request-input tool. Send
one message with a blocker or question to the parent (or owner named in the
brief), then end the turn.

Implement bounded acceptance with behavior proof; use
[write-tests](../write-tests/SKILL.md) for test design. For a bug fix, watch the
regression test fail on pre-fix code and record where in Notes; a test that passes
without the fix proves nothing. For migrations,
authorization, locking/concurrency or policy logic, list edge cases in Acceptance
and map each to a test or recorded evidence. Observe required UI/runtime behavior
within actual permissions. Required acceptance that remains unobserved stays open
with a next-action owner. Record tested revisions, observations, commands,
results, trade-offs and limits in Notes or human Validation; evidence from an
earlier head cannot establish a current gate verdict.

Run a command expected to take more than about two minutes (builds, browser
runs, full checks) detached, so a harness that ends your turn cannot kill it:
`setsid nohup bash -c 'echo $$ >LOG.pid; <cmd> >LOG 2>&1; echo $? >LOG.exit' </dev/null >/dev/null 2>&1 &`,
then wait for `LOG.exit`. Do not end the turn, settle or report done while a job
you started has no `LOG.exit`; if the turn must end, first schedule a one-shot
wakeup that reads `LOG.exit` and resumes the delivery step it feeds. To cancel, check `LOG.exit` first, then run
`kill -- -$(cat LOG.pid)`; never `pkill -f`, which also matches your own shell
(`$!` is not the job: `setsid` may fork). Codex 0.165 sends SIGTERM (exit 143) to such
children (openai/codex#46702); drop this once a fixed release is verified.

Treat /tmp as disposable: only running-command logs, build output and browser
runs belong there. Keep receipts, evidence, review artifacts and handoffs on the
PR or issue, or in the durable scratch your repository's instructions name. Make
task checkouts as worktrees of an existing clone, not new clones, and remove
yours once its PR merges or closes and everything is pushed.

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget). Check the clean committed head
before pushing; publish and review every pushed head within that budget. Batch
all substantive and mechanical repairs into one delta head. Preserve an approved
head unless necessary changes require renewed checks and review. Suggestions
must not expand scope. Merge only with authority; otherwise finish at readiness.
Never merge main into a reviewed branch. Rebase your own commits onto
origin/main BEFORE review. If a conflict appears AFTER review: rebase (drop any
merge), check, then one fresh whole-PR primary; that is the PR's one fresh lineage.

Do not bypass the sandbox, change the check list or enable `[checks].network`
to pass checks. Report base failures rather than work around them. Only you
commit, never checks, hooks or scripts. If acceptance requires unlisted paths,
update task and PR first; request scope decisions for expanded proof seams or
ownership. If interrupted, preserve an adoptable diff and report remaining
acceptance, checks and findings under the contract's handoff rules.

After a merge/queue failure, an authorized requeue means posting `@mergifyio queue <queue>` as the whole PR comment from the same login first, then rerunning `loopzero merge --wait`, which never requeues by itself.
