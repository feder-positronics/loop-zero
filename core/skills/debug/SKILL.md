---
name: debug
description: >-
  Diagnose broken behavior by reproducing it, testing competing hypotheses, and
  proving a root cause before code changes. Use when the cause remains unclear
  after initial reproduction. For a failing test with an understood cause use
  `fix-failing-tests`.
---

# Debug

Produce a falsifiable root-cause explanation and the smallest evidence-backed
fix contract. Keep diagnosis separate from implementation.

## Contracts

- Remain read-only unless the user or outer workflow authorizes a durable report
  or a bounded diagnostic mutation. A standalone authorized write follows the
  [worktree rule]({{package.rules_root}}/parallel-agents.mdc).
- Apply the [security rule]({{package.rules_root}}/security.mdc) to logs, database state,
  production-derived data, credentials, tokens, and PII.
- Load the [debug reference](./reference.md) only for {{package.product_name}}-specific isolated
  environments, logs, backend/frontend paths, or durable-learning destinations.

## Evidence

- Capture the exact scenario, environment, expected and observed behavior, and
  the smallest reliable reproduction. Reuse relevant issue or incident evidence
  when available.
- Maintain plausible competing hypotheses and choose experiments that
  distinguish them. The model owns experiment order; follow the evidence across
  UI, API, service, persistence, worker, and external boundaries only as needed.
- Confirm the cause by predicting and observing behavior. A suspicious line,
  correlation, or absence of unverified logs is not proof.
- Record rejected hypotheses, blast radius, and the evidence limits. If proof is
  impossible, identify the exact missing evidence or authority.

## Authority and Routes

- Do not edit production code, restart remote services, mutate production or
  production-derived state, change issue/lifecycle state, or publish durable
  learnings without the authority required for that action. Prefer synthetic or
  local evidence; a production-derived experiment must be explicitly in scope
  and use its named cleanup/recovery contract.
- Route a proven UI defect to {{skill_routes.fix_ui_bug.link}}, a failure
  whose remaining work is test repair to
  [`fix-failing-tests`](../fix-failing-tests/SKILL.md), backend/frontend changes
  to {{skill_routes.implement_backend.link}} or
  {{skill_routes.implement_frontend.link}}, competing responses to
  [`resolve-findings`](../resolve-findings/SKILL.md), and diff assessment to
  [`code-review`](../code-review/SKILL.md).

## Done When

Return the reproduction, discriminating evidence, rejected hypotheses,
confirmed cause, blast radius, smallest fix and validation contract, and any
unresolved evidence or authority limit. Stop before implementation.
