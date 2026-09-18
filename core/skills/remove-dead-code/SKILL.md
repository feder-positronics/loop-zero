---
name: remove-dead-code
description: Remove evidenced dead code in a small recoverable batch, verify it, and either keep the removal or restore it; use for supplied candidates or a manual census, not live-code cleanup.
disable-model-invocation: true
---

# Remove Dead Code

Remove one evidenced batch of dead code without losing a recoverable state.
Do not use this skill to simplify live code or redesign a module.

## Grounding

Use only candidates the requester supplied or a bounded manual census found.
When invoked inside a loop-zero task, inherit its worktree; otherwise require a
task worktree created with `loopzero start`. Record the base and current head,
then read [the removal reference](reference.md).

For every candidate, verify absence of static callers and inspect possible
entrypoints, configuration, registration, reflection, serialization, generated
code, public API use, documentation, and tests. Local uncommitted work is
context, not proof that code is unreachable in the task's base.

Classify each candidate as clear removal, live-code refactor, or manual review.
Exclude ambiguous and dynamically reached code from removal. Route live-code
cleanup to `refine-code` and design-dependent removal to `write-design-doc`.

## Recoverable Removal

The order is load-bearing:

1. Establish a clean rollback point and choose a small, coherent batch of only
   clear candidates.
2. Quarantine the batch as a reviewable removal diff, retaining its exact prior
   content in Git. Do not mix refactoring or unrelated cleanup into the diff.
3. Inspect build, packaging, generated-artifact, and documentation consequences,
   then run `loopzero check` against the quarantined state.
4. Keep the removal only when every relevant check passes. On failure, restore
   the batch to its rollback point and classify it for refactor or manual review;
   do not debug while it remains half-removed.

Do not infer that an ambiguous symbol is dead. Preserve it with the evidence
that prevented confirmation.

## Exit

Exit when every candidate is either a validated removal, restored, or preserved
with a reason; the worktree remains recoverable; and `.loopzero/task.md` Notes
record the inspected base/head, evidence, validation, restored candidates, and
remaining limits. Hand the bounded diff to the normal PR and review workflow;
do not create a separate queue, progress file, or sidecar report.
