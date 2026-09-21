---
name: work-issue
description: Deliver one live GitHub issue from duplicate-checked intake through acceptance, draft PR, independent review, and verified merge or partial delivery; use only for work anchored to an issue.
---

# Work Issue

Take one live GitHub issue through the six loop-zero commands to a verified merge
and either closure or an explicit partial-delivery boundary. Preserve the issue
as intake context and the PR as delivery record.

Use the shared [model selection and delegation guidance](../../../docs/DELEGATION.md)
when splitting work or choosing an executor. Read the task repository's
`models.toml` for model and effort; delegate useful bounded units through the
available runtime.

## Intake

1. Read the issue body, comments, labels, linked PRs, screenshots, logs, and
   referenced artifacts. State the outcome, affected surfaces, non-goals,
   validation evidence, and unsafe unknowns. Ask one focused question only when
   the issue cannot answer a material acceptance choice.
2. Check GitHub for an existing open or merged PR, branch, or prior partial
   delivery for this exact issue. If the issue is closed, a delivery PR exists,
   or work already shipped, reconcile that state; do not duplicate implementation.
   Verify the GitHub remote before mutation and the connected environment before
   relying on any user-reported production state.
3. Run `loopzero start issue-<number>-<slug>`. In `.loopzero/task.md`, translate
   the issue into observable Acceptance lines and keep the issue link in Notes.
   Put `Closes #N` in Notes only when this PR completes the issue; use `Refs #N`
   for a partial umbrella-issue delivery.

## Deliver

4. Implement only the accepted scope, add proof for its behavior, and run
   `loopzero check`. Fix failures without weakening the configured checks. Read
   [the delivery safeguards](reference.md) for conditional API, fresh-stack,
   documentation, blueprint, re-entry, and closeout requirements.
5. Commit the bounded change, then run `loopzero pr` to push and open the draft
   PR. Before publication, repeat the duplicate check; stop and reconcile if a
   competing delivery appeared.
6. Run `loopzero review` for the independent primary review. Address every open
   `critical` and `important` thread, reply with the fix commit, rerun
   `loopzero check`, and use the single delta review allowed by the contract.
7. Run `loopzero ready --wait`; it waits for the required checks on this head.
   Never write a `gh` polling loop. Do not bypass readiness or resolve a finding
   silently.
8. Recheck issue and competing-PR state, then run `loopzero merge --wait`. Do not close
   an umbrella issue from a partial delivery.

## Stops

- If acceptance expands beyond one PR, propose issue boundaries and continue
  only with the one the requester selects.
- If the issue conflicts with current code or another delivery, report the exact
  conflict before editing.
- Requester-reserved, outward, costly, irreversible, or scope-expanding choices
  stop for explicit direction; issue-local reversible choices may proceed.
- If a required post-merge condition cannot yet be observed, report it as open;
  do not claim closure.

## Exit

Done means the acceptance lines for this delivery are satisfied, its PR is
merged, no blocking review thread remains, and `loopzero merge` has completed
branch and worktree cleanup. For a closing delivery, verify `Closes #N` and that
GitHub shows the issue closed; for a `Refs #N` partial delivery, verify the issue
remains open and report the remaining acceptance. Report the issue, PR, merge
SHA, validation, thread state, verified remote issue and PR state, and the
connected environment state when acceptance depends on it.
