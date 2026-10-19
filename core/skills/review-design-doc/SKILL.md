---
name: review-design-doc
description: Critique an existing design artifact for product fit, architecture, decisions, and readiness without editing it.
---

# Review Design Doc

Decide whether the design is sound and sufficient for its intended next action.
Read [shared design guidance](../write-design-doc/reference.md) for grounding
and decision status, then [review lenses](reference.md). Identify type, audience,
status, next actor, and scope. Apply only checks material to that action; do not
manufacture alternatives or demand detail the artifact's type does not need.

Challenge premises and current-state claims against evidence. Probe edge cases,
failures, and migration; flag internal inconsistencies. Respect settled decisions
unless new evidence contradicts their premise.

Return evidence-backed findings using [contract severities](../../CONTRACT.md#findings):
unsafe, invalid, or infeasible design is critical; a material decision, boundary, acceptance,
or validation gap that threatens the next action is important. Suggestions
do not block.

Give each finding a stable short ID, severity, evidence anchor, consequence, and smallest
resolving acceptance condition. Separate wording edits from design findings;
finish with an overall assessment, review limits, and the next action or exact blocker.

Leave the artifact unchanged. Return findings in the response; use existing
PR review threads only when recording there is authorized. Do not create a
findings file. Resolve response-only findings within the design workflow;
[resolve-findings](../resolve-findings/SKILL.md) handles existing PR threads.
Route edits through [refine-design-doc](../refine-design-doc/SKILL.md), missing
first-principles design through [write-design-doc](../write-design-doc/SKILL.md).
