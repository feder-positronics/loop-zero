---
name: refine-code
description: Simplify a named or current-task diff, or delete evidenced dead code when explicitly commissioned, while preserving supported contracts.
---

# Refine Code

Simplify the user-named or current-task diff while preserving observable behavior
and public contracts. Follow [the contract](../../CONTRACT.md) in the outer task's
worktree; confirm branch and task before editing. Prefer clear names, control
flow and established local patterns without unrelated cleanup or compatibility
layers.

Treat concurrency, exception timing, transactions, ordering, query predicates,
persistence, isolation, serialization, public imports, API shape and assertions
as semantic until equivalence is demonstrated. Route new behavior, abstraction
boundaries or weakened tests to [implement](../implement/SKILL.md).

Deletion of code outside the current task diff requires explicit requester
commissioning. Choose a clean rollback commit before a deletion batch; if any
check fails, restore the whole batch to it before debugging.

For deletion, establish that supported consumers, including dynamic entry points,
registries, reflection, plugins, configuration, generated consumers and public
exports, do not reach the code. Inspect base and current tree; local edits cannot
prove base unreachability. Lint and coverage alone do not prove deadness, nor does
one dead symbol prove its module dead. Preserve uncertain candidates and explain
why. Keep removal recoverable in Git and validate affected runtime, build,
packaging, deployment and documentation boundaries.

Explain the clarity gain and any apparently behavioral edit. Run focused success
and relevant failure/cancellation tests and contract checks; record evidence and
limits in task Notes.

After this step, resume the commissioned outcome within the authorized finish,
or hand over its remaining acceptance; after two unsuccessful repairs,
reconsider the route.
