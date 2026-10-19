---
name: implement
description: Implement a bounded change through behavior proof and readiness; merge only when separately authorized.
---

# Implement

Read [the contract](../../CONTRACT.md), `.loopzero/task.md`, repository
guidance, and affected public interfaces. Follow [credential conventions](../../CREDENTIALS.md)
for API keys and [delegation guidance](../../../docs/DELEGATION.md) when choosing
executors or splitting work.

## Deliver

1. Edit only in the `loopzero start` task worktree; confirm its branch and task
   file first.
2. Write or extend behavior tests before or alongside code at the narrowest
   stable public seam a real caller uses. If proof requires private reach-ins
   or test-only extraction, report the missing seam.
   For migrations, authorization, locking/concurrency, or policy/verdict logic,
   first list edge cases in Acceptance and map each to tests or recorded
   evidence, e.g. existing/deleted/duplicate rows, concurrent writers, lock order,
   authorization boundaries, rule precedence.
3. Assert observable outputs, state, or boundary effects independently of the
   implementation, not mirrored calculations, mock calls, or internal steps.
   **Watch regression tests fail on pre-fix code and record the failing output
   line in task Notes.** A test passing without the fix proves nothing.
4. Iterate with focused checks; commit small steps describing the change,
   without unrelated formatting. Record trade-offs and deliberately skipped
   work in Notes before committing.
5. Run `loopzero check` on the clean committed head **before pushing or `loopzero pr`**.
   Fix failures without changing the check list, bypassing the sandbox, or
   enabling `[checks].network`. Host test runs support iteration; only the
   sandboxed report counts as delivery proof.
6. Run `loopzero pr`, then the primary `loopzero review`. Fix valid
   `critical`/`important` findings; batch **all repairs, including format,
   lint and renames, into the one delta head**. Commit, check, publish, and
   review every pushed head under the [review budget](../../CONTRACT.md#review-budget).
   Reply per thread with `loopzero resolve <id> "<fix commit and change>"`,
   or a verified reason it does not apply. Suggestions must not expand scope.
7. Stop at `loopzero ready --wait` unless merge is authorized. When authorized
   and readiness exits 0, run `(cd <worktree> && loopzero merge --wait)` from
   the repository root in the same turn. Follow the contract's wait and
   exit-code rules.

## Boundaries

- If acceptance requires unlisted paths, update the task and PR before continuing.
- Report checks failing on base without this change; do not work around them.
- If a faithful public proof seam expands acceptance or ownership, record the
  boundary and request that scope decision.
- If time expires before readiness, preserve an adoptable diff and report its
  exact state, remaining checks and unresolved findings.
- Only you commit; never commit from checks, hooks or scripts.
