---
name: plan
description: Bound a requested change with objective, acceptance, ownership, affected paths, and validation before implementation when those are not yet clear.
---

# Plan

Turn one request into a change small enough for one PR. Do not implement it.

Use the shared [model selection and delegation guidance](../../../docs/DELEGATION.md)
when splitting work or choosing an executor. Read the task repository's
routing source (`models.toml`, or its named equivalent) for model and effort;
delegate useful bounded units through the
available runtime.

Read [the contract](../../CONTRACT.md), repository guidance, the relevant issue
or `.loopzero/task.md`, and enough code to verify current behavior and ownership.

## Do

1. State the objective in one paragraph. If the request contains two
   independent outcomes, split it into two tasks. For state machines,
   migrations, auth, or concurrency, one PR changes one behavioral invariant,
   and its acceptance names that invariant's callers, readers, and transitions.
   Generated code (SDKs, lockfiles) goes in its own mechanical commit. A slice
   beyond roughly 1,500 hand-written changed lines is split, or the task file
   says why it cannot be: a reviewer samples a diff that large, it does not
   verify it.
2. Write acceptance as observable conditions: a test that passes, a command
   whose output changes, a page that renders. No adjectives.
3. List the paths you expect to touch, their current owner or governing
   constraint, and the checks from `workflow.toml`
   that exercise them. Add a new check only if no existing one covers the
   behavior. When the request splits into several tasks, list the paths for
   each; two tasks that share a file are stacked or done in sequence, never
   in parallel from the same base.
4. Decide routine implementation choices from available evidence.
   Ask only when a decision would change the acceptance criteria or touch a
   surface someone else owns.
5. Confirm that validation runs through `loopzero check`, whose sandbox isolates
   the worktree and Git metadata, and that the task uses the single primary and
   optional delta review allowed by the contract.
6. Write the objective, acceptance, affected paths, constraints, checks, and
   owner into `.loopzero/task.md` (see [HANDOFF](../../HANDOFF.md)). No separate
   plan file.

## Stop when

- Missing authority, conflicting ownership, or a material requirement needs a
  decision. Name the exact decision
  and the options; continue with any preparation that does not depend on it.
- The change cannot fit one PR. Propose the split and the order.
- Exit when the task file contains one objective, observable acceptance, owned
  scope, and the checks that will prove it.

## Do not

- Plan extra review rounds or tracking documents. The contract already
  fixes one primary review plus one delta.
- Plan work outside the requested change to "clean up while here".
