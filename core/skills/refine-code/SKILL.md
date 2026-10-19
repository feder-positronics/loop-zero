---
name: refine-code
description: Simplify a user-named or current-task diff while preserving observable behavior and public contracts.
---

# Refine Code

Make the bounded diff clearer through names, control flow, fewer redundant
intermediates and established local patterns. Default to current-task files
unless the requester names a broader boundary.

Follow [the contract](../../CONTRACT.md) in the task worktree, inheriting the
outer task's worktree when nested. Confirm branch and `.loopzero/task.md`
before editing.

Remove comments or symbols only with evidence they have no runtime, import,
configuration or tooling effect. One dead symbol does not prove its file or
module dead.

Treat concurrency, exception timing, transactions, ordering, query predicates,
persistence, isolation, serialization, public imports, API shape and test
assertions as semantic until equivalence is demonstrated. Stop and route new
behavior, abstraction boundaries, weakened tests or uncertain deletion to
[implement](../implement/SKILL.md).

Explain the concrete clarity gain and any apparently behavioral edit. Run focused
success and relevant failure/cancellation tests, then contract delivery checks.
Record validation in `.loopzero/task.md`. Finish with passing
checks and no unrelated cleanup or compatibility layer.
