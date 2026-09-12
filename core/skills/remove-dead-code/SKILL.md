---
name: remove-dead-code
description: >-
  Remove evidenced dead code through quarantine, verification, and explicit
  confirmation or rollback. Use for candidates produced by an audit or manual
  census. For live-code cleanup use `refine-code`.
disable-model-invocation: true
---

# Remove Dead Code

Turn evidenced dead-code candidates into verified removals without losing a
recoverable state.

## Contracts

- Invocation authority (user or the autonomous improvement loop, on its own
  cadence) is defined once in the
  [skill README's Maintenance Pipelines note]({{package.skills_readme}}#maintenance-pipelines).
- Own a dedicated worktree when invoked directly; nested runs inherit their
  outer workflow's worktree under the [parallel-agents rule]({{package.rules_root}}/parallel-agents.mdc).
- Load the [removal reference]({{package.docs_root}}/guides/reference/remove-dead-code-reference.md)
  for queue locations, merged-state evidence, commands, gates, and report shape.
- Route live-symbol cleanup to [`refine-code`](../refine-code/SKILL.md), a
  behavior-preserving live-module split to
  [`decompose-module`](../decompose-module/SKILL.md), redesign needing a durable
  contract to [`write-design-doc`](../write-design-doc/SKILL.md), and weak or
  stale audit inputs back to [`audit-health`](../audit-health/SKILL.md).

## Safety Sequence

The order is load-bearing:

1. Establish the merged-state evidence baseline and a rollback point. Classify
   each candidate as quarantine, refactor, or manual review; exclude ambiguous
   entrypoints and dynamic dispatch from removal batches.
2. Quarantine only clear candidates with the canonical script, in a small
   reviewable batch. Never delete them directly.
3. Run every relevant gate from the reference against the quarantined state.
4. Confirm the batch only when all gates pass. On any failure, roll back
   immediately; do not debug in a half-quarantined state.
5. Preserve refactor/manual-review items and produce the prune report.

## Evidence and Authority

- Cite the inspected `origin/main` SHA and evidence for every removal; local
  uncommitted work is context, not proof of merged-state reachability.
- Record confirmed removals, rollbacks, preserved candidates, validation, and
  remaining limits in the report.
- Do not infer that an ambiguous or dynamically reached symbol is dead. Leave
  it unchanged for manual review or a separately authorized investigation.

## Done When

- Every candidate is confirmed, rolled back, or preserved with a reason.
- Confirmation happened only after the relevant gates passed, the worktree is
  recoverable, and the canonical prune report is complete.
