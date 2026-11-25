---
name: plan
description: Plan PR-sized tasks with observable acceptance, ownership, paths and checks. File authorized slices from active goals or planned blueprints.
---

# Plan

Read [the contract](../../CONTRACT.md), repository guidance, the issue/task and
relevant code. Use [delegation guidance](../../../docs/DELEGATION.md) for executors.

Before slicing, restate the ask in one precise sentence and check it against the
issue or request. Choose a coherent, reviewable acceptance slice. Split or
sequence when coupling, uncertainty or ownership makes integration hard. Scale
planning depth to the cost of reversing a choice later: spend planning on
persisted state, CLI or config interfaces, trust boundaries, public APIs and
shared abstractions—asking "if this assumption changes after merge, what
existing state or callers must still work?" (use a compact state table when
transitions matter)—and settle cheap, reversible choices during implementation.
When a cheap observation on real data or the real environment could change the design
or its target, sequence that observation first and derive targets from it. Give
each task an objective, observable acceptance, affected paths, owners, governing
constraints and proving `workflow.toml` checks. Add checks only when existing
checks cannot cover behavior.

When authorized to file slices from an active goal or planned blueprint, use its
tracking issue as the parent. Give each PR-sized task a plain issue title and
use the task shape above for its body. Name the parent step and the evidence it
needs in an `Unblocks: #<parent> (step N) — <evidence>` line. Link each child with
`gh api "repos/$repo/issues/$parent/sub_issues" -F sub_issue_id="$id"`. Use
`owner/repo` for `repo`, the parent issue number for `parent`, and the child's
numeric `id` from the REST response for `id`.

Record the plan in `.loopzero/task.md` using [HANDOFF](../../HANDOFF.md), without
another tracking file. Resolve routine choices from evidence; missing authority,
conflicting ownership or material requirements need the exact decision and options.
Continue independent preparation. Do not implement or plan unrelated cleanup.

Then continue the task you were given as far as it authorizes, or hand over
what is left of its acceptance.
