---
name: refine-design-doc
description: >-
  Converge an existing exploration, ADR, initiative, or blueprint by cycling
  design review, decision resolution, and focused edits until no critical or
  important findings remain. Use when a deposited design artifact must reach
  review-clean. To resolve recorded findings into decisions without editing the
  artifact use `resolve-findings`.
---

# Refine Design Doc

Make the artifact ready for its intended next action without reopening ratified
decisions or forcing a fixed number of review cycles.

Use a dedicated worktree when standalone under the [parallel-agent rule]; nested
delivery inherits its orchestrator worktree.

[parallel-agent rule]: {{package.rules_root}}/parallel-agents.mdc

## Contract

Read the complete artifact, upstream Decision Records, and relevant current
state. Locked decisions remain constraints unless new evidence triggers formal
reconsideration; Assumed decisions remain active and vetoable.

Apply the [exhaustive convergence exception]: all critical and important
findings must be surfaced and resolved. Invoke each [review-design-doc] pass in
exhaustive critical/important mode; the five-finding signal budget does not
apply to those severities in this workflow.

[exhaustive convergence exception]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md#signal-budget
[review-design-doc]: ../review-design-doc/SKILL.md

Repeat only while material findings remain:

1. run `review-design-doc` on the current artifact;
2. resolve competing responses through [resolve-findings] and [{{skill_tokens.doctrine_d18}} authority];
3. apply focused edits that address accepted findings;
4. verify the finding, not merely the wording, is resolved.

The agent chooses edit order and may combine tightly coupled findings. Do not
expand scope, manufacture options, or turn suggestions into blockers.

Run one bounded cross-harness pass on the converged document only when explicitly
requested with a nonempty reason under the [cross-harness rule]. If the same
important finding survives twice, revisit the chosen approach; after five non-converging cycles, stop and escalate.

[resolve-findings]: ../resolve-findings/SKILL.md
[{{skill_tokens.doctrine_d18}} authority]: {{package.docs_root}}/design/owner-doctrine.md
[cross-harness rule]: {{package.rules_root}}/cross-harness-review.mdc

## Exit Criteria

- [ ] No critical/important findings remain.
- [ ] Locked decisions stayed settled unless new evidence formally reopened them.
- [ ] Decisions/open questions are durable and acceptance intent is actionable.
- [ ] Lifecycle/frontmatter and the relevant [design-doc validation] pass,
  including `{{toolchain.commands.docs_verify}}`.
- [ ] Outer workflow ownership of worktree/lifecycle/commit was not duplicated.

[design-doc validation]: ../write-design-doc/SKILL.md#tracking-and-validation
