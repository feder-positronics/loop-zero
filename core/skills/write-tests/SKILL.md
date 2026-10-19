---
name: write-tests
description: Add or improve behavior-focused coverage when testing is the task; use fix-failing-tests for observed failures.
---

# Write Tests

Add the smallest useful tests for a named user or system contract and regression
risk. Follow [the contract](../../CONTRACT.md) in the task worktree, inheriting
the outer task's worktree when nested. Confirm branch and `.loopzero/task.md`
before editing. Do not change product behavior merely to increase coverage.

Choose the cheapest faithful boundary: unit tests for deterministic logic with
stable collaborators and no real network/database; integration tests for real
queries, transactions, isolation, constraints, serialization and adapters;
end-to-end tests for critical journeys that cannot be proved below the assembled
system.

Exercise stable public seams. Repeated private access or a harness as complex
as the behavior signals a missing seam: report it rather than extracting
production code solely for testing convenience. Use an independent oracle for
observable outputs, transitions or boundary effects. Mock calls, internal steps,
snapshots or duplicated implementation calculations alone are insufficient.

Cover applicable success, denial/isolation, validation, transitions and failures
without a case quota. Follow repository placement, fixture, marker and naming
conventions. Keep fixtures minimal, isolated and deterministic; avoid timing
tolerance, shared mutable state, duplicate cases and assertions repeating mock
configuration.

For a bug fix or behavior change, observe the regression test failing on
pre-fix code for the intended behavior and record the failing line in
`.loopzero/task.md` Notes. An infrastructure failure does not count.
Coverage-only tests may pass existing code.

Run focused tests during development, then the narrow neighboring suite and
contract delivery checks. Record commands and results in task Notes. Exit when
regression failure evidence exists, checks pass and added brittleness is lower
than the risk protected. Route
necessary product changes to [implement](../implement/SKILL.md).
