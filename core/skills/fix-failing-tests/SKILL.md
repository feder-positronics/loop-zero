---
name: fix-failing-tests
description: Repair an understood failing test at the smallest correct code, test, or environment owner; use diagnose when the cause needs proof and write-tests for new coverage.
---

# Fix Failing Tests

Restore trustworthy test evidence without changing the intended product
contract merely to make the suite green.

Work in the task worktree from `loopzero start`, or the outer task's worktree
when nested; confirm its branch and `.loopzero/task.md` before editing.

## Repair Loop

1. Reproduce the failure with the narrowest documented repository command that
   preserves it, without output filters that hide its exit status. Capture the
   assertion or error, environment, worktree state, and exit status.
2. Establish the intended contract from acceptance, repository guidance,
   neighboring tests, production behavior, and history. Classify the owner as
   production regression, stale or incorrect test, or environmental/flaky
   isolation defect. A flaky family with an onset date stays a regression
   candidate until the changes landed at that onset are compared; use
   [diagnose](../diagnose/SKILL.md) for that comparison.
3. Change the smallest correct owner. Do not delete coverage, weaken an
   assertion, or broaden timing or retry tolerance without evidence that the
   prior oracle was wrong.
4. Rerun the exact reproducer, then only the neighboring tests implied by the
   changed dependency surface. A broad green suite does not replace focused
   proof. Finish by committing the fix and running `loopzero check` on the
   clean head.

For an explicitly requested full-suite cleanup, collect the failure set, group
by demonstrated root cause, repair one coherent group at a time, and rerun the
full suite only at group boundaries. Preserve the first unresolved failure and
command evidence if the suite cannot become green.

Do not install or update shared dependencies merely because a tool is missing;
use the documented setup and frozen-lockfile path only when dependency state is
demonstrably stale. Treat runner loss, missing tools, network failure, and
sandbox failure as infrastructure evidence, not a reason to alter the product
or test.

## Exit

Stop for a material product decision rather than choosing it silently. Route a
failure whose cause remains unproven to [diagnose](../diagnose/SKILL.md) and net-new coverage to
[write-tests](../write-tests/SKILL.md).

Standalone fixes continue through the normal PR workflow; formal review is
`loopzero review`.

Exit with each failure's classification and evidence, changed owner, focused
and expanded commands with exit status, remaining limits, and whether the
requested target is green. Record results in `.loopzero/task.md`; no accepted
fix weakens the oracle without evidence that it was wrong, or leaves an
unexplained deletion.
