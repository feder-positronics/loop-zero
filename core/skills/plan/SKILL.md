---
name: plan
description: Bound an unclear change into PR-sized tasks with observable acceptance, ownership, paths and checks; do not implement.
---

# Plan

Read [the contract](../../CONTRACT.md), repository guidance, the issue/task file,
and enough code to verify behavior and ownership. Use
[delegation guidance](../../../docs/DELEGATION.md) when choosing executors or
splitting work.

## Define the task

- State one objective per PR; split independent outcomes and give their order.
  For state machines, migrations, auth or concurrency, change one behavioral
  invariant per PR; name its callers, readers and transitions in acceptance.
- **Give every task observable acceptance**: passing tests, changed command
  output or rendered pages, without adjectives.
- **Put generated code, including SDKs and lockfiles, in a separate mechanical
  commit.**
- Split beyond roughly 1,500 hand-written changed lines, or explain in the
  task why splitting is impossible: reviewers sample such diffs rather than
  verify them.
- For each task, list affected paths, owners/governing constraints and
  `workflow.toml` checks. Add a check only if existing checks cannot cover the
  behavior. Tasks sharing a file must be stacked or sequential, never parallel
  from the same base.
- Decide routine choices from evidence; ask only when acceptance changes or
  another owner's surface is affected.

Write objective, acceptance, paths, constraints, checks and owner in
`.loopzero/task.md` using [HANDOFF](../../HANDOFF.md), without a separate plan
file. Exit when it contains one objective, observable acceptance, owned scope
and proving checks.

Missing authority, conflicting ownership or material requirements need the
exact decision and options; continue independent preparation. If one PR cannot
fit, propose the split and order. Do not implement, add review rounds/tracking
documents, or plan unrelated cleanup.
