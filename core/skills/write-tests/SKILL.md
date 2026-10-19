---
name: write-tests
description: Design, add, or improve tests when testing itself is the task; use for coverage gaps, suite quality, or complex test design, and use fix-failing-tests for observed failures.
---

# Write Tests

Add the smallest set of behavior-focused tests that materially improves
confidence. Do not change product behavior merely to increase coverage.

Work in the task worktree from `loopzero start`, or the outer task's worktree
when nested; confirm its branch and `.loopzero/task.md` before editing.

## Choose The Proof

- Name the user or system contract, regression risk, and failures the suite must
  distinguish.
- Choose the cheapest faithful boundary: unit tests for deterministic logic
  with stable collaborators (no real database or network); integration tests for
  queries, transactions, isolation, constraints, serialization, and adapters
  whose behavior depends on real boundaries; end-to-end tests only for the few
  critical journeys that cannot be proved below the assembled system.
- Exercise stable public seams. Repeated private reach-ins or a harness as
  complex as the behavior is evidence of a missing seam; report that boundary
  rather than extracting production code solely for test convenience.
- Use an oracle independent of the implementation. Assert observable outputs,
  state transitions, or boundary effects; avoid asserting only mock calls,
  internal steps, snapshots, or a duplicated implementation calculation.
- Cover applicable success, denial or isolation, validation, state transition,
  and failure behavior without turning that list into a quota.

## Validate And Exit

Run the focused test while developing. For a bug fix or behavior change, watch
it fail on the pre-fix code for the intended regression and record that failing
line in `.loopzero/task.md`. Coverage-only tests may pass on existing code.
Then run the narrow neighboring suite, commit, and run `loopzero check` on the
clean head. Follow repository placement, fixture, marker, and naming
conventions. Keep fixtures minimal, isolated, and deterministic, and preserve
the runner's exit status. Avoid timing tolerance, shared mutable state,
duplicate cases, and assertions that restate how a mock was configured.

Exit when every added test protects a named contract, regression tests were
seen failing on the pre-fix code for the intended behavior rather than
infrastructure, relevant checks pass, and
the added brittleness is lower than the risk protected. Record commands and
results in `.loopzero/task.md`; use [implement](../implement/SKILL.md) if
production behavior must change.
