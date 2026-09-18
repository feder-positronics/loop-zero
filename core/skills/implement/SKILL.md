---
name: implement
description: Deliver a bounded change with the tests that prove it, run the repository's checks, and open a draft PR.
---

# implement

Read [the contract](../../CONTRACT.md) and `.loopzero/task.md`. Make the
smallest complete change that satisfies every acceptance line.

## Do

1. Work only in the worktree created by `loopzero start`. Confirm with
   `loopzero status` before the first edit.
2. Write or extend the test that proves the behavior before or alongside the
   code. A change without a proof is not complete.
3. Run `loopzero check` before pushing. Fix nonzero exits; do not edit the
   check list to make them pass.
4. Commit in small steps with messages that state what changed. Keep the
   diff free of unrelated formatting churn.
5. Fill Notes in `.loopzero/task.md` with trade-offs and deliberately skipped
   work, then run `loopzero pr`.
6. After `loopzero review`, fix every `critical` and `important` finding in
   its thread, push, rerun `loopzero check`, and request the delta review.
   Reply in the thread with what changed; do not resolve a thread silently.
7. Stop at `loopzero ready`. Merge only when the task says so.

## Stop when

- Acceptance turns out to need a change outside the listed paths. Update the
  task file and say so in the PR before continuing.
- A check fails on the base branch without your change. Report it as a base
  failure; do not work around it in this PR.

## Do not

- Skip the sandbox or run checks with network to make them pass.
- Address suggestions by expanding scope; note them and move on.
- Commit from inside a check, hook or script. Only you commit.
