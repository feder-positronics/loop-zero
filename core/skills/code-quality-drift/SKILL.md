---
name: code-quality-drift
description: >-
  Review backend, frontend, or both for drift from canonical rules and for
  module boundaries that expose nearly as much complexity as they hide.
  Produce a calibrated, chunked proposal report without changing code. For a
  specific diff or PR use `code-review`.
disable-model-invocation: true
---

# Code Quality Drift

Test explicit architectural hypotheses against the repository. Avoid catalog-
walking ceremony and do not confuse size/density signals with defects.

## Grounding

Invocation authority (user or the autonomous improvement loop, on its own
cadence) is defined once in the
[skill README's Maintenance Pipelines note]({{package.skills_readme}}#maintenance-pipelines).
Choose `be`, `fe`, or `all`; resume a compatible recent audit when available.
Read relevant canonical rules, [suppressions], recent [health queues], and current
graph summaries. Use the [contract-prune runbook] only when contract-removal
evidence is in scope. Rebuild stale graphs with `{{toolchain.commands.audit_graph_backend}}`,
`{{toolchain.commands.audit_graph_frontend}}`, or `{{toolchain.commands.audit_graph}}`. A standalone sweep uses a dedicated
worktree under the [parallel-agent rule]. Load the [drift reference] for resume
gates, evidence lenses, report state, suppressions, and routing.

## Method

Maintain these outcomes without forcing phase or chunk order:

- Resume a compatible plan or start fresh; re-verify carried observations.
- Record quantitative baselines and the few strongest explicit hypotheses.
- Select evidence-bearing chunks and track honest scope coverage.
- Test module-deepening hypotheses with the deletion test in the [drift
  reference]; prioritize a named upcoming change or recent churn without
  treating either as proof of a defect.
- Classify confirmed observations as enforcement lag, canonical gap, tooling
  gap, or acknowledged exception.
- Verdict each hypothesis `confirmed / wrong / partial`; update priors and drop
  dead lenses.
- Synthesize cross-cutting themes once and record concrete routing.

The agent chooses chunk order and depth. Responsibility density, PageRank,
cycles, casts, fetches, and rule greps are leads only; proposals require
file/symbol evidence and concrete impact.

Follow the [producer boundary]. When a drift lens belongs in a recursive
campaign, route the lens and its evidence to that campaign tracker; the owning
`audit-surface` pass evaluates it as a section of its single inspection pass.

## Output

Write `{{package.audit_root}}/<date>/drift-<mode>/report.md` with scope coverage, ranked
proposals, hypothesis outcomes, exceptions, and routing. Do not implement.
Canonical gaps become rule-authorship proposals; dead code routes to
[remove-dead-code]; stable backend/Python extraction boundaries to
[decompose-module]; structural consolidation or deepening that changes ownership,
interfaces, or caller contracts to [write-design-doc]; and local clarity to
[refine-code]. Route changed trust boundaries
to [security-review], competing responses to [resolve-findings], and a material
scope ambiguity to [align]. Keep each accepted proposal or decision on its
owning tracker or design surface; the report is not a shadow backlog.

Record only observed skill friction/bloat in the reflection stream. A sweep is
not required to manufacture a process lesson.

## Exit Criteria

- [ ] Quantitative baseline and scope/chunk coverage are recorded.
- [ ] Hypotheses have confirmed/wrong/partial verdicts with evidence.
- [ ] Important proposals, canonical gaps, and facts have durable owners/routes.
- [ ] Campaign-relevant lenses are routed to the owning tracker and the
  producer boundary is honored.
- [ ] Exceptions, dead lenses, collector limits, and cross-cutting themes are
  explicit.
- [ ] No application code changed.

[parallel-agent rule]: {{package.rules_root}}/parallel-agents.mdc
[drift reference]: reference.md
[suppressions]: {{package.suppressions_file}}
[health queues]: ../audit-health/SKILL.md
[contract-prune runbook]: {{package.docs_root}}/guides/infra/contract-prune-runbook.md
[remove-dead-code]: ../remove-dead-code/SKILL.md
[decompose-module]: ../decompose-module/SKILL.md
[write-design-doc]: ../write-design-doc/SKILL.md
[refine-code]: ../refine-code/SKILL.md
[security-review]: ../security-review/SKILL.md
[resolve-findings]: ../resolve-findings/SKILL.md
[align]: ../align/SKILL.md
[producer boundary]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md#finding-producer-boundary
