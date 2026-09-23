---
name: fix-failing-tests
description: Diagnose observed test failures, decide whether code, test, or environment violates the intended contract, and apply the smallest correct fix; for new coverage use write-tests.
---

# Fix Failing Tests

Restore trustworthy test evidence without changing the intended product
contract merely to make the suite green.

For standalone work, use the task worktree created by `loopzero start` and
confirm its branch and `.loopzero/task.md` before editing. Nested work stays in
the outer task's worktree.

Read [the validation reference](reference.md) when choosing narrow-to-broad
commands or handling missing dependencies.

## Repair Loop

1. Reproduce the failure with the smallest command that preserves it. Capture
   the assertion or error, environment, worktree state, and exit status.
2. Establish the intended contract from acceptance, repository guidance,
   neighboring tests, production behavior, and history. Classify the owner as
   production regression, stale or incorrect test, or environmental/flaky
   isolation defect.
3. Change the smallest correct owner. Do not delete coverage, weaken an
   assertion, or broaden timing or retry tolerance without evidence that the
   prior oracle was wrong.
4. Rerun the exact reproducer, then only the neighboring tests implied by the
   changed dependency surface. A broad green suite does not replace focused
   proof. Finish with `loopzero check`.

For an explicitly requested full-suite cleanup, collect the failure set, group
by demonstrated root cause, repair one coherent group at a time, and rerun the
full suite only at group boundaries. Preserve the first unresolved failure and
command evidence if the suite cannot become green.

## Exit

Stop for a material product decision rather than choosing it silently. Route an
unknown product symptom to [diagnose](../diagnose/SKILL.md) and net-new coverage to
[write-tests](../write-tests/SKILL.md).

Standalone fixes that are broad or change product behavior require
[code-review](../code-review/SKILL.md) after checks pass.

Exit with each failure's classification and evidence, changed owner, focused
and expanded commands with exit status, remaining limits, and whether the
requested target is green. Record results in `.loopzero/task.md`; no accepted
fix weakens the oracle or leaves an unexplained deletion.
