---
name: review-design-doc
description: >-
  Review an existing exploration, ADR, initiative, blueprint, or design doc for
  product fit, architecture, decisions, and implementation readiness. Use for
  critique or validation before implementation. To converge the document through
  edits use `refine-design-doc`.
---

# Review Design Doc

Judge whether the artifact is the right design and a sufficient contract for
its intended next action. Leave the reviewed artifact unchanged unless the user
explicitly requests approved edits; required review evidence and finding
deposits remain in scope.

## Grounding

Read the complete artifact, its upstream decisions, only the implementation
needed to verify current-state claims, and relevant [Vision], [owner doctrine],
and [users/jobs]. Identify its type, audience, status, intended next actor, and
scope boundary.

[Vision]: {{package.docs_root}}/design/vision.md
[owner doctrine]: {{package.docs_root}}/design/owner-doctrine.md
[users/jobs]: {{package.docs_root}}/design/users-and-jobs.md

## Required Checks

Select only those material to the artifact:

- [ ] Crystallized need-now evidence, actors/jobs, outcome, non-goals, and the
  rejected simpler shape (fields-on-existing before new entities/endpoints);
- [ ] For Vision edits, its [Generality contract in owner doctrine D-16]: each
  principle must survive an implementation replacement and name no current
  feature, schedule, or component; route mechanism prose to a guide or ADR as an
  `important` finding;
- [ ] Under [VIS-5], flag any design choice that silently forecloses either
  per-seat SaaS or usage-based API revenue shape;
- [ ] Data/state ownership, API/auth/isolation, lifecycle, migration/rollback;
- [ ] Composition, consumers, UI/nav/catalog fit, observability/operations;
- [ ] Domain terms and user-facing labels follow the
  [write-design-doc authoring contract] rather than creating a parallel context
  glossary;
- [ ] Option quality, rejected alternative, decision authority/reversibility, and
  unresolved uncertainty;
- [ ] Temporary scaffolding has removal inside the artifact's own scope;
- [ ] Acceptance criteria, validation, file/test touchpoints, and implementation
  freedom versus unnecessary prescription.

[Generality contract in owner doctrine D-16]: {{package.docs_root}}/design/owner-doctrine.md
[VIS-5]: {{package.docs_root}}/design/vision.md
[write-design-doc authoring contract]: ../write-design-doc/SKILL.md#design-contract

Layer crossing, unwired capability, speculative entities/endpoints, silent
temporary scaffolding, and contradiction with ratified decisions are findings.

## Output

Report evidence-backed `critical / important / suggestion` findings using the
[canonical finding schema], followed by a short assessment and review limits.
Keep wording edits separate from design findings. Emit `agent_event output`;
run the bounded [cross-harness document pass] only when standalone and explicitly
requested with a nonempty reason. Persist and deposit local anchored findings
through `record-findings`; governed dispatches deposit automatically.

[canonical finding schema]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md
[cross-harness document pass]: {{package.rules_root}}/cross-harness-review.mdc

Route competing responses to `resolve-findings`; convergence to
`refine-design-doc`; missing first-principles design to `write-design-doc`.

## Exit Criteria

- [ ] Artifact intent, type, scope, and blueprint boundary were respected.
- [ ] Material design/readiness gaps are ranked and actionable.
- [ ] Approved edits preserve SSoT links instead of copying detail.
- [ ] Validation and review limits are explicit.
- [ ] Local anchored findings were deposited through `record-findings`.
- [ ] The next workflow can proceed without guessing.
