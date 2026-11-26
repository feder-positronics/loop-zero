---
name: refine-code
description: Simplify a named or current-task diff, or delete evidenced dead code when explicitly commissioned, while preserving supported contracts.
---

# Refine Code

Preserve observable behavior and public contracts in the user-named or current-task diff.
Confirm task and branch in the outer worktree and follow [the contract](../../CONTRACT.md).
Use clear names, control flow and local patterns without unrelated cleanup or compatibility layers.

Treat concurrency, exception timing, transactions, ordering, query predicates,
persistence, isolation, serialization, public imports, API shape and assertions
as semantic until equivalence is demonstrated. Route new behavior, abstraction
boundaries or weakened tests to [implement](../implement/SKILL.md).

Deletion outside the task diff requires explicit commissioning by the requester. Treat [code health](../../../docs/CODE-HEALTH.md)
findings as leads. Select a clean rollback commit and restore the whole batch before debugging any failed check.
Verify in the base and current trees that supported consumers cannot reach removed code. Check dynamic entry
points, registries, reflection, plugins, configuration, generated consumers and public exports.
Lint, coverage, post-edit searches and one dead symbol alone do not prove base or module unreachability.
Explain and preserve uncertain candidates. Keep removals recoverable in Git and validate affected runtime,
build, packaging, deployment and docs boundaries.

Explain the clarity gain and any apparently behavioral edit. Run focused success
and relevant failure/cancellation tests and [contract checks](../../CONTRACT.md#blockers); record evidence and
limits in task Notes.
