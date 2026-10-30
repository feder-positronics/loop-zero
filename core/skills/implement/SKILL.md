---
name: implement
description: Implement a bounded change through behavior proof and readiness; merge only when separately authorized.
---

# Implement

Read [the contract](../../CONTRACT.md), `.loopzero/task.md`, repository guidance
and affected interfaces. Confirm the task worktree and branch. Follow
[credential conventions](../../CREDENTIALS.md) and
[delegation guidance](../../../docs/DELEGATION.md).

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

Follow the contract's [delivery steps](../../CONTRACT.md#the-six-steps) and
[review budget](../../CONTRACT.md#review-budget). Check the clean committed head
before pushing; publish and review every pushed head within that budget. Batch
all substantive and mechanical repairs into one delta head. Preserve an approved
head unless necessary changes require renewed checks and review. Suggestions
must not expand scope. Merge only with authority; otherwise finish at readiness.

Do not bypass the sandbox, change the check list or enable `[checks].network`
to pass checks. Report base failures rather than work around them. Only you
commit, never checks, hooks or scripts. If acceptance requires unlisted paths,
update task and PR first; request scope decisions for expanded proof seams or
ownership. If interrupted, preserve an adoptable diff and report remaining
acceptance, checks and findings under the contract's handoff rules.
