---
name: implement
description: Deliver an understood bounded change with its owning behavior proof and repository-required checks, stopping at readiness unless merge is separately requested.
---

# Implement

Deliver one bounded change with its behavior proof. Read [the contract](../../CONTRACT.md),
`.loopzero/task.md`, repository guidance, and the affected public interfaces.
When the task uses API keys, follow [the credential convention](../../CREDENTIALS.md)
and document the integration's variable, local fallback, and target environment.

Use the shared [model selection and delegation guidance](../../../docs/DELEGATION.md)
when splitting work or choosing an executor. Read the task repository's
`models.toml` for model and effort; delegate useful bounded units through the
available runtime.

## Do

1. Work only in the task worktree created by `loopzero start`. Confirm its
   branch and `.loopzero/task.md` before the first edit.
2. Write or extend a test that proves the behavior before or alongside the code.
   Exercise the narrowest stable public seam available to a real caller. If only
   private reach-ins or a test-only extraction make the test possible, report
   the missing seam instead of disguising it as coverage.
3. Make the test oracle independent of the implementation: assert observable
   outputs, state, or boundary effects, not the same calculation, mock calls, or
   internal steps used by the code. A regression test counts only after you
   watched it fail on the pre-fix code; record that failing output line in
   `.loopzero/task.md` Notes. A test that passes without the fix proves nothing.
4. Run focused checks while iterating, then `loopzero check` before pushing.
   Fix nonzero exits; do not edit the
   check list to make them pass. Running the test command directly is for
   iteration only: the sandbox hides host tools that CI also lacks, so only
   the `loopzero check` report counts as proof.
5. Commit in small steps with messages that state what changed. Keep the
   diff free of unrelated formatting churn.
6. Fill Notes in `.loopzero/task.md` with trade-offs and deliberately skipped
   work, then run `loopzero pr`.
7. After `loopzero review`, fix every `critical` and `important` finding in
   its thread, push, and rerun `loopzero check`. Mechanical format, lint, or
   rename repairs with unchanged behavior need no delta review; substantive
   repairs get the one delta review. Reply with what changed; do not resolve a
   thread silently.
8. Stop at `loopzero ready`. Merge only when the task says so.

## Stop when

- Acceptance turns out to need a change outside the listed paths. Update the
  task file and say so in the PR before continuing.
- A check fails on the base branch without your change. Report it as a base
  failure; do not work around it in this PR.
- The public seam needed for faithful proof would expand acceptance or ownership.
  Record the boundary and ask for that scope decision.
- Time expires before readiness. Preserve an adoptable diff and report its exact
  state, remaining checks, and unresolved findings.

## Do not

- Skip the sandbox or run checks with network to make them pass.
- Address suggestions by expanding scope; note them and move on.
- Commit from inside a check, hook or script. Only you commit.
