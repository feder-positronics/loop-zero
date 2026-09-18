---
name: write-tests
description: Design, add, or improve tests when testing itself is the task; use for coverage gaps, suite quality, or complex test design, and use fix-failing-tests for observed failures.
---

# Write Tests

Add the smallest set of behavior-focused tests that materially improves
confidence. Do not change product behavior merely to increase coverage.

For standalone work, use the task worktree created by `loopzero start` and
confirm its branch and `.loopzero/task.md` before editing. Nested work stays in
the outer task's worktree.

Read [the test-depth reference](reference.md) when boundary choice or suite
quality needs more detail.

## Choose The Proof

- Name the user or system contract, regression risk, and failures the suite must
  distinguish.
- Choose the cheapest faithful boundary: unit for isolated logic, integration
  for real boundary behavior, and end-to-end only for critical cross-boundary
  journeys.
- Exercise stable public seams. Repeated private reach-ins or a harness as
  complex as the behavior is evidence of a missing seam; report that boundary
  rather than extracting production code solely for test convenience.
- Use an oracle independent of the implementation. Assert observable outputs,
  state transitions, or boundary effects; avoid asserting only mock calls,
  internal steps, snapshots, or a duplicated implementation calculation.
- Cover applicable success, denial or isolation, validation, state transition,
  and failure behavior without turning that list into a quota.

## Validate And Exit

Run the focused test while developing, demonstrate that it fails for the
intended bad behavior, then run the narrow neighboring suite and
`loopzero check`. Keep fixtures minimal and deterministic and preserve the
runner's exit status.

Exit when every added test protects a named contract, fails for the intended
regression rather than infrastructure, relevant checks pass, and the added
brittleness is lower than the risk protected. Record commands and results in
`.loopzero/task.md`; use [implement](../implement/SKILL.md) if production
behavior must change.
