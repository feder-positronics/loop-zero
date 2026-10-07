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
when splitting work or choosing an executor.

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
   `.loopzero/task.md` Notes. A regression test that passes without the fix
   proves nothing.
4. Run focused checks while iterating. Commit in small steps with messages that
   state what changed; keep the diff free of unrelated formatting churn. Fill
   `.loopzero/task.md` Notes with trade-offs and deliberately skipped work
   before committing.
5. Run `loopzero check` on the clean committed head before pushing. Fix nonzero
   exits; do not edit the check list to make them pass. Running the test command
   directly is for iteration only: the sandbox exposes only system directories,
   the configured `ro_paths`, the allowlisted environment and, unless enabled,
   no network, so a command that passes on the host can fail there; only the
   `loopzero check` report counts as proof.
   Never filter a `loopzero` command through a pipe; its exit code is the
   verdict (see the contract's Failure section).
6. Run `loopzero pr`.
7. After the primary `loopzero review`, fix every valid `critical` and
   `important` finding, commit, rerun `loopzero check`, and push with
   `loopzero pr`. Every pushed head needs review, so put mechanical format,
   lint, or rename repairs in the same push as the substantive repairs and spend
   the one delta review on that head. Reply in each thread with
   `loopzero resolve <id> "<fix commit and what changed>"`, or with the reason a
   finding does not apply; do not resolve a thread silently.
8. Stop at `loopzero ready --wait`; it waits for the required checks, so never
   write a `gh` polling loop. Merge only when the task says so; from the
   repository root, run `(cd <worktree> && loopzero merge --wait)`.

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

- Skip the sandbox or enable `[checks].network` to make checks pass.
- Address suggestions by expanding scope; note them and move on.
- Commit from inside a check, hook or script. Only you commit.
