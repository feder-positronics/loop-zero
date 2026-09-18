---
name: review
description: Assess an artifact, system, architecture, workflow, agent configuration, or idea and return ranked concerns without implementing changes; for code diffs use code-review.
---

# Review

Assess one non-diff target at the requested altitude and return a ranked,
evidence-backed judgment. Do not implement changes.

## Route And Ground

Route a code diff or PR to [code-review](../code-review/SKILL.md), adding
[security-review](../security-review/SKILL.md) when a trust boundary changes.
For other targets, read enough source material and surrounding context to
understand purpose, constraints, callers or consumers, and acceptance.

Use an opinion pass for directional requests: a concise assessment, the few
highest-value concerns, and a recommended direction. Use a findings review only
when the requester asks for an audit, exhaustive assessment, or gate input.

Select only relevant lenses:

- architecture: boundaries, coupling, contracts, and divergent paths;
- strategy: problem, users, evidence, leverage, and timing;
- workflow: outcome coverage, friction, enforcement, and feedback;
- agent configuration: source-of-truth consistency, trigger overlap,
  deterministic versus prompt enforcement, and stale scaffolding.

For agent configuration, keep an instruction only when it adds non-inferable
value. Exact choreography needs a safety reason or observed failure.

## Output And Exit

For an opinion pass, return the assessment, ranked concerns, recommended
direction, and material uncertainty. For a findings review, return only the
highest-value evidence-backed findings as `critical`, `important`, or
`suggestion`, plus one synthesis and the review limits.

Leave the target unchanged. Exit when scope and altitude are explicit, each
concern is supported by inspected evidence, and uncertainty is stated. If the
review belongs to a live task, keep actionable discussion in its issue or PR
threads; do not create a separate findings store.
