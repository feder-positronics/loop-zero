---
name: fix-failing-tests
description: Repair an understood test failure; route unproven causes to diagnose and new coverage to write-tests.
---

# Fix Failing Tests

Restore trustworthy test evidence while preserving the intended product contract.
Follow [the contract](../../CONTRACT.md) in the task worktree, inheriting the
outer task's worktree when nested. Confirm branch and `.loopzero/task.md` before editing.

1. Reproduce with the narrowest documented command preserving the failure.
   Capture assertion or error, environment, worktree state and exit status.
2. Establish intended behavior from acceptance, repository guidance, neighboring
   tests, production behavior and history. Prove whether production, the test
   oracle, or environment/isolation owns the defect. Unproven causes go to
   [diagnose](../diagnose/SKILL.md). A flaky family remains a regression candidate
   until changes landed at its onset have been compared.
3. Repair the smallest correct owner. Deleting coverage, weakening assertions,
   raising timeouts, or broadening timing/retry tolerance requires evidence
   that the previous oracle was wrong.
4. Rerun the exact reproducer and neighboring tests implied by the changed
   dependency surface. A broad green suite cannot replace focused proof.
   Complete delivery checks under the contract.

For explicitly requested full-suite cleanup, collect failures, group by
demonstrated cause, repair one coherent group at a time, and rerun the full
suite at group boundaries. If unresolved, preserve the first failing command
and its evidence.

Runner loss, missing tools, network and sandbox failures are infrastructure
evidence, not grounds to alter product behavior or tests. Do not install or
update shared dependencies merely because a tool is missing; use documented
setup and frozen-lockfile procedures when dependency state is demonstrably stale.

Stop for material product decisions. Route new coverage to
[write-tests](../write-tests/SKILL.md). Record each failure's classification,
evidence, changed owner, focused and expanded commands with exit statuses,
remaining limits and whether the requested target is green in `.loopzero/task.md`.
Leave no unexplained deletion or unsupported oracle weakening.
