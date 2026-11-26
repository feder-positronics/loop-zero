---
name: write-tests
description: Add or improve behavior-focused coverage when testing is the task or guide test design during implementation; use diagnose for observed failures.
---

# Write Tests

Protect a named behavior and regression risk with the smallest faithful tests.
Follow [the contract](../../CONTRACT.md). Do not change product behavior merely
to increase coverage; route product repairs to [implement](../implement/SKILL.md)
and observed failures to [diagnose](../diagnose/SKILL.md).

Choose the cheapest faithful public boundary: unit for deterministic logic, integration for real
storage/adapters, and end to end for assembled journeys. Report missing seams without extracting code
just for test convenience. Use independent oracles for outputs, transitions and effects. Mock calls,
snapshots, internal steps and copied calculations are insufficient alone.

Cover material success, denial/isolation, validation, transitions and failures. Follow repo conventions
with minimal deterministic, isolated fixtures. Prefer cheap real producers or match the public fields
and behavior consumed, avoiding shared mutable state, timing tolerance and duplicate cases.
For a bug fix, observe the regression test fail on pre-fix code. If impossible, explain the oracle and its limits.
Infrastructure failure cannot prove regression detection. New or changed tests block only through the
consumer's admission rule. Established critical assertions remain blocking. Coverage-only tests may pass existing code.

Run focused and affected neighboring tests, then [contract delivery checks](../../CONTRACT.md#blockers).
Record commands, results and limits in task Notes.
