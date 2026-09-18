---
name: refine-code
description: Simplify recently touched code without intentional behavior, API, data, or test-contract change; use after a bounded edit or before code review.
---

# Refine Code

Make one user-named or current-task diff easier to read and maintain while
preserving its observable contract.

For standalone work, use the task worktree created by `loopzero start` and
confirm its branch and `.loopzero/task.md` before editing. Nested work stays in
the outer task's worktree.

## Refine

- Default to files changed by the current task unless the requester names a
  broader boundary. Do not sweep unrelated modules.
- Prefer clearer names and control flow, fewer redundant intermediates, and
  reuse of an established local pattern.
- Remove comments or unused symbols only when their lack of runtime, import,
  configuration, or tooling effect is demonstrated. Do not infer from one
  symbol that its whole file or module is dead; that requires separate proof.
- Treat concurrency, exception timing, transactions, ordering, query
  predicates, persistence, isolation, serialization, public imports, API shape,
  and test assertions as semantic until equivalence is demonstrated.
- If cleanup requires new behavior, a new abstraction boundary, weakened tests,
  or uncertain deletion, stop and route it to [implement](../implement/SKILL.md)
  rather than disguising it as refinement.

## Validate And Exit

Describe the concrete clarity gain and any edit that could appear behavioral.
Run focused tests for success and relevant failure or cancellation paths, then
`loopzero check`. Exit when observable and public contracts are unchanged,
checks pass, and no unrelated cleanup or compatibility layer was introduced.
Record the validation in `.loopzero/task.md` and leave review to
[code-review](../code-review/SKILL.md).
