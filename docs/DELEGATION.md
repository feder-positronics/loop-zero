# Delegating implementation to a coding agent

Guidance for an orchestrating agent that hands loop-zero tasks to a delegate
such as `codex exec` or a Claude subagent. Learned on 2026-09-18 while
landing five changes with Codex delegates; see the feature plan in
[design/2026-09-18-feature-plan.md](design/2026-09-18-feature-plan.md).

## When to delegate

- Delegate a task above roughly 50 source lines. Below that, the brief plus
  the review costs about as much as writing the change.
- Give each delegate its own `loopzero start` worktree. Never two delegates in
  one worktree.
- Two tasks that touch the same file are done in sequence or stacked, never in
  parallel from the same base. The `plan` skill lists paths per task for this.

## The brief

Inline the full design note or task file; the delegate has no other context.
State the rules it cannot infer:

- Only the `loopzero check` report counts as proof. Running the test command
  directly is for iteration; the sandbox hides host tools that CI also lacks.
- Do not commit unless the git directory is writable to it. In a linked
  worktree the shared `.git` lives outside a workspace-write sandbox; either
  pass it as an extra writable directory or let the orchestrator commit.
- Report the line delta of `src/` and any deviation from the design. Treat
  "no deviations" as unverified until the diff says so.

## After the delegate returns

1. Read the diff yourself. Two of five reports today were wrong: one hid an
   env-var change, one claimed skipped tests that did not exist.
2. Run the suite once with a bare `PATH` that has no reviewer binaries.
3. Commit, then run the normal six commands. `loopzero review` is the
   independent check; do not replace it with your own read.
4. Under the merge queue, `loopzero merge` is two runs: enqueue, then verify.

## Do not

- Run two test suites concurrently on one host; pytest's temp-dir rotation
  deletes the other run's directory.
- Delete a stacked PR's base branch before retargeting the PR above it;
  GitHub closes the upper PR and it cannot be reopened.
