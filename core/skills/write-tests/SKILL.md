---
name: write-tests
description: >-
  Design, add, or improve backend and frontend tests when testing itself is the
  task. Use for coverage gaps, suite quality, or complex test design. For fixing
  currently failing tests use `fix-failing-tests`.
---

# Test Development

Add the smallest set of behavior-focused tests that materially improve
confidence. Do not translate coverage numbers or generic checklists into tests.

Use a dedicated worktree under the [parallel-agent rule] when standalone;
nested delivery inherits its orchestrator worktree.

## Contract

- Identify the user/system contract, regression risk, and failure modes the
  suite must distinguish.
- Choose the cheapest faithful boundary: pure unit/component for logic and
  wiring; real DB/integration only for queries, transactions, isolation,
  constraints, or workflows that mocks cannot prove; E2E only for critical
  cross-boundary journeys.
- When faithful coverage needs repeated private reach-ins or a harness nearly as
  complex as the behavior, treat that as missing-seam evidence rather than
  extracting a pure function only for testability. Stop and report the missing
  seam: a user-requested broader survey belongs to
  [`code-quality-drift`](../code-quality-drift/SKILL.md), while a proven stable
  Python boundary belongs to [`decompose-module`](../decompose-module/SKILL.md).
- Backend unit tests never use DB fixtures. Preserve mock/autospec and fixture
  isolation rules. Frontend tests prefer observable roles and behavior over
  implementation structure.
- Cover meaningful happy, denial/isolation, validation, state-transition, and
  failure cases only where applicable. Avoid assert-the-mock, snapshot noise,
  duplicate cases, and tests written solely to move coverage.
- Use the five-core success/auth/isolation/validation/denial model as a
  relevance prompt, never a quota. Keep at most two markers, deterministic
  minimum-valid fixtures, clear Arrange/Act/Assert, and autospec-safe mocks.

Run the focused test while developing, then the narrow suite that proves
integration with neighboring behavior. Apply local backend/frontend rules and
use the [test-development reference] for placement, budgets, markers, and
project-specific patterns.

## Exit Criteria

- [ ] Every test protects a named contract or likely regression.
- [ ] It fails for the intended bad behavior, not broken infrastructure.
- [ ] Relevant focused/suite checks pass.
- [ ] Marker, fixture, mock, ordering, and coverage-gaming anti-patterns are absent.
- [ ] Suite brittleness added is lower than the risk protected.

[parallel-agent rule]: {{package.rules_root}}/parallel-agents.mdc
[test-development reference]: reference.md
