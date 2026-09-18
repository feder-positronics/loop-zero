---
name: review-design-doc
description: Review an existing design artifact for product fit, architecture, decisions, and implementation readiness; use for critique before implementation, not for editing the artifact.
---

# Review Design Doc

Review one existing exploration, ADR, initiative, blueprint, or design document
as a read-only artifact. Decide whether it is the right design and a sufficient
contract for its intended next action.

## Grounding

Read the complete artifact, its linked issue or `.loopzero/task.md`, upstream
decisions, repository guidance, and only the implementation needed to verify
current-state claims. Identify its type, audience, status, next actor, scope,
and settled versus open decisions before judging it.

Read [the review reference](reference.md) and apply only the checks material to
the artifact. Respect ratified decisions unless current evidence contradicts
their premise. Do not manufacture options or penalize an artifact for detail
that its type does not need.

## Findings

Report only evidence-backed findings:

- `critical`: the design is unsafe, internally invalid, or cannot meet its goal;
- `important`: implementation would likely ship a defect or lacks a material
  decision, boundary, acceptance criterion, or validation route;
- `suggestion`: worthwhile improvement that does not block the next action.

For each finding, give a stable short ID, evidence anchor, consequence, and the
smallest acceptance condition that resolves it. Keep wording edits separate
from design findings. Follow findings with a short overall assessment and the
limits of the review.

Leave the artifact unchanged unless the requester separately authorizes edits.
Return findings in the response; when the artifact belongs to an existing PR
and recording the review there is authorized, use its review threads rather
than a local findings file. Resolve competing responses to response-only
findings within the design workflow; use `resolve-findings` only when the
findings already exist as PR review threads. Route convergence through edits to
`refine-design-doc`, and missing first-principles design to `write-design-doc`.

## Exit

Exit when the artifact's intent and scope have been respected, every material
design or readiness gap is ranked and actionable, validation and review limits
are explicit, and the next actor can proceed or can see the exact blocker.
