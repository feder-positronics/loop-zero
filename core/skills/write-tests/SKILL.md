---
name: write-tests
description: Add or improve behavior-focused coverage when testing is the task; use diagnose for observed failures.
---

# Write Tests

Protect a named behavior and regression risk with the smallest faithful tests.
Follow [the contract](../../CONTRACT.md). Do not change product behavior merely
to increase coverage; route product repairs to [implement](../implement/SKILL.md)
and observed failures to [diagnose](../diagnose/SKILL.md).

Choose the cheapest faithful public boundary: unit tests for deterministic logic,
integration tests for real storage/adapters and end-to-end tests for journeys
that need the assembled system. Report missing seams rather than extracting
production code solely for testing convenience. Use independent oracles for
outputs, transitions and effects; mock calls, snapshots, internal steps or copied
implementation calculations alone are insufficient.

Cover material success, denial/isolation, validation, transitions and failures.
Follow repository conventions with minimal, deterministic, isolated fixtures;
avoid shared mutable state, timing tolerance and duplicate cases. For bug fixes,
observe the regression test fail on pre-fix code; only when that is impossible,
explain the oracle and its limits. Infrastructure failures do not demonstrate
regression detection.
Coverage-only tests may pass existing code.

Run focused and affected neighboring tests, then contract delivery checks.
Record commands, results and limits in task Notes.

After this step, resume the commissioned outcome within the authorized finish,
or hand over its remaining acceptance; after two unsuccessful repairs,
reconsider the route.
