---
name: review
description: Assess a non-diff artifact, system, architecture, workflow, agent configuration, or idea without changing it.
---

# Review

Return a ranked, evidence-backed judgment at the requested altitude.
Route diffs to [code-review](../code-review/SKILL.md), adding
[security-review](../security-review/SKILL.md) for changed trust boundaries.
For designs, read [design guidance](../write-design-doc/reference.md) and
[design lenses](design-reference.md). Judge sufficiency for the
next actor, respecting settled decisions unless their premises fail. Separate
wording from material defects; do not demand detail irrelevant to that action.
Return design findings in the response; publish to PR threads only when authorized.

Read the target and context for purpose, constraints, consumers, and acceptance.
Question premises, trace material claims to sources, probe edge cases and
failure modes, and identify inconsistencies. Each concern needs evidence,
consequence, and a concrete direction.

Use a concise opinion pass for directional requests: assessment, highest-value
concerns, recommended direction. Use findings for requested audits, exhaustive
assessment, or gate input.

Select relevant lenses:

- Architecture: boundaries, coupling, contracts, divergent paths.
- Strategy: problem, users, evidence, leverage, timing.
- Workflow: outcome coverage, friction, enforcement, feedback.
- Agent configuration: source-of-truth consistency, trigger overlap,
  deterministic versus prompt enforcement, stale scaffolding. Keep instructions
  only for non-inferable value; choreography needs a safety reason or observed failure.

State scope, uncertainty, and limits. For findings, use
[contract severities](../../CONTRACT.md#findings) and one synthesis.
Leave the target unchanged. Do not create findings files. Live-task findings belong in PR review threads;
other actionable discussion belongs in its issue or PR, never a separate store.

After this step, resume the commissioned outcome within the authorized finish,
or hand over its remaining acceptance; after two unsuccessful repairs,
reconsider the route.
