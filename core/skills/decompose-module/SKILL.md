---
name: decompose-module
description: >-
  Split an oversized or responsibility-dense Python module along an evidenced,
  stable boundary without changing behavior or caller contracts. Use when a
  module's size or mixed responsibilities block safe work. For evidenced
  dead-code removal use `remove-dead-code`.
---

# Decompose Module

Reduce responsibility density through the smallest cohesive extraction that
keeps public behavior, transactions, and imports stable. Size is only a lead.

## Contracts

- Own a dedicated worktree when standalone and inherit the outer mutating
  workflow's worktree when nested under the
  [parallel-agents rule]({{package.rules_root}}/parallel-agents.mdc).
- Apply the [backend rule]({{package.rules_root}}/be.mdc), the [python314 rule]({{package.rules_root}}/python314.mdc),
  and the standalone/nested [Review Gate]({{package.rules_root}}/review-gate.mdc).
- Route dead candidates to [`remove-dead-code`](../remove-dead-code/SKILL.md),
  behavior-neutral local cleanup to [`refine-code`](../refine-code/SKILL.md),
  and feature behavior to [`implement-backend`](../implement-backend/SKILL.md).

## Evidence and Method

- Map responsibilities, imports, callers, tests, shared state, transaction
  ownership, private reach-ins, cycles, and likely growth before choosing a
  boundary. A line count alone is not evidence.
- Proceed only when a concern has a stable seam such as distinct callers, a
  test boundary, reusable responsibility, or genuinely mixed orchestration.
  Otherwise report why no safe extraction is warranted and make no edit.
- Apply a depth check to the proposed boundary: it must reduce what callers
  need to know by hiding meaningful responsibility behind a smaller stable
  interface. If collapsing it back would only move lines or expose the same
  knowledge — especially an extraction made solely for direct unit testing —
  the new module would be shallow, so make no edit.
- Extract one cohesive concern at a time, validate the moved seam, and remove
  the old path immediately. Do not add indefinite shims, a new class hierarchy,
  behavior change, or opportunistic cleanup to make the split appear successful.
- Record reversible boundary choices under
  [D-18]({{package.docs_root}}/design/owner-doctrine.md).
  Stop for owner direction before a Reserved architecture or public-contract
  change.

## Done When

- The responsibility/coupling map demonstrates a clearer boundary, focused
  tests prove the moved seam and caller behavior, and relevant import,
  collection, type, lint, and risk-scoped broader checks pass.
- Public imports, transactions, router/OpenAPI behavior, and callers remain
  unchanged; no cycle, duplicate path, compatibility architecture, or unrelated
  cleanup remains. If no stable seam exists, the evidence-backed no-edit report
  is the completed outcome.
