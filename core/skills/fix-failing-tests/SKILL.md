---
name: fix-failing-tests
description: >-
  Diagnose one or more failing tests, decide whether production code, the test,
  or its environment violates the intended contract, and apply the smallest
  correct fix. Use for observed backend or frontend test failures, including an
  explicitly requested full-suite cleanup. For new coverage use `write-tests`.
---

# Fix Failing Tests

Restore trustworthy test evidence without changing the intended product
contract merely to make the suite green.

## Contracts

- A standalone mutation follows the [worktree rule]({{package.rules_root}}/parallel-agents.mdc);
  nested work inherits the outer orchestrator's worktree and review ownership.
- Apply the scoped [backend test]({{package.docs_root}}/guides/backend/backend-testing.md)
  or [frontend test]({{package.docs_root}}/guides/frontend/frontend-testing.md) contract.
  Load the [command reference](./reference.md) for the narrow-to-broad lanes.
- Apply the [review gate]({{package.rules_root}}/review-gate.mdc) at the outermost workflow.

## Safe Sequence

Classification must precede mutation, and the exact reproducer must pass before
broader evidence can count:

1. Reproduce the failure with the smallest command that preserves it. Capture
   the assertion/error, environment, and relevant working-tree context without
   hiding the command's exit status.
2. Establish the intended contract from acceptance criteria, canonical docs or
   rules, neighboring tests, production behavior, and change history. Classify
   the failure as a production regression, stale/incorrect test, or
   flaky/environmental isolation defect. If evidence leaves a material product
   choice unresolved, stop for that decision instead of choosing silently.
3. Change the smallest correct owner. Preserve or strengthen the assertion's
   behavioral protection; do not delete coverage, loosen an assertion, or make
   timing/retry tolerance broader without evidence that the old oracle was wrong.
4. Re-run the exact reproducer, then expand only to tests implied by the changed
   dependency surface. A wider passing suite never substitutes for the focused
   proof.

For an explicitly requested full-suite cleanup, collect the complete failure
set, group failures by demonstrated root cause, repair and validate one coherent
wave at a time, and rerun the full suite only at wave boundaries. Preserve the
first unresolved failure and command evidence if the suite cannot become green.

## Authority and Routes

- Keep fixes inside the tested contract. Route an unknown production symptom to
  [`debug`](../debug/SKILL.md), net-new coverage work to
  [`write-tests`](../write-tests/SKILL.md), and planned product behavior to the
  applicable implementation workflow.
- Do not install or update shared dependencies merely because a binary is
  missing. Follow the repository's serialized dependency and frozen-lockfile
  setup contract from the command reference.
- Standalone broad or behavior-changing fixes receive
  [`code-review`](../code-review/SKILL.md). Documentation settlement belongs to
  the outer workflow or [`design-handoff`](../design-handoff/SKILL.md) only when
  its trigger applies.

## Done When

Report each failure's classification and evidence, changed owner, focused and
expanded commands with exit status, remaining failures or limits, and whether
the requested target/full suite is green. No accepted fix weakens the oracle or
leaves an unexplained deletion.
