# Code Quality Drift — {{package.product_name}} Reference

## Run State And Resume

Reports live at `{{package.audit_root}}/YYYY-MM-DD/drift-<be|fe|all>/`. Follow the
[parallel-agent rule] rather than creating runtime-specific worktrees manually.
Default runs keep the main report to the ten highest-value routed proposals;
`--long-run` covers the declared scope comprehensively and records every derived
chunk as reviewed, condensed, or deferred with a reason.

Resume the newest compatible incomplete `DRIFT-PLAN.md` only when it is at most
seven days old, its `graph_summary_commit` is at most 50 commits behind `HEAD`,
and no scoped file was deleted since that commit. Reuse its calibration,
coverage, and baseline; re-verify every carried file/symbol before routing.
Otherwise start a new dated report.

[parallel-agent rule]: {{package.rules_root}}/parallel-agents.mdc
## Evidence Selection

Derive chunks from the current tree with `rg --files`; groups of roughly 15–30
files are useful when they preserve a responsibility boundary, not a quota.
Generated files are not hypothesis candidates by default: inspect generator
inputs, source contracts, or consumers. Interleave backend and frontend evidence
when a full-stack hypothesis crosses the boundary.

Prefer a user-named upcoming change as the selection bias. Otherwise use recent
commit history as a tiebreaker among comparably strong graph, rule, and health
leads; churn selects where to look, never what verdict to return.

Load canonical `.cursor/rules/` for the selected mode: backend uses `be.mdc`,
`security.mdc`, `observability.mdc`, `repo-structure.mdc`, and
`review-gate.mdc`; frontend uses `fe.mdc`, `ts.mdc`, `security.mdc`, and
`review-gate.mdc`. Derive lenses from current rules instead of copying them.

High-value {{package.product_name}} lenses:

- Contract honesty: trace schema/router → service/model → generated SDK/action;
  classify a mismatch as `prune-candidate | wire | enforce | keep`. Never scan
  generated OpenAPI output as if it were a caller.
- Ownership split: coverage, fixture health, markers, missing route fallbacks,
  and dead test files belong to [audit-health]; rule violations inside tests and
  helpers reaching into service privates belong here.
- Raw `fetch()` is a classification lead: protected-data access is drift;
  stable framework, proxy, or download boundaries can be exceptions.
- Transaction-boundary exceptions such as bootstrap, maintenance,
  commit-before-enqueue, and worker-owned sessions require evidence, not counts.

Read an existing API contract queue only for routing. The canonical queue and
commands belong to the [contract-prune runbook]; `wire` and `blocked` never
become removal entries.
[contract-prune runbook]: {{package.docs_root}}/guides/infra/contract-prune-runbook.md
[audit-health]: ../audit-health/SKILL.md
## Classification And Routing

- `enforcement lag` — a rule exists; cite it and route to the narrowest fix.
- `canonical gap` — no rule covers the demonstrated pattern; route to a design
  or rule decision before implementation.
- `tooling gap` — enforcement cannot detect an existing rule; propose tooling
  and a suppression only when needed.
- `acknowledged exception` — propose a [suppressions] entry with evidence;
  never append one silently during the sweep.

Route dead files/modules to `remove-dead-code`, stable backend/Python extraction
boundaries to `decompose-module`, frontend boundaries needing a new abstraction
to `write-design-doc`, and confirmed consolidation/deepening that changes an
owner, interface, or caller contract to `write-design-doc` before implementation.
Route local clarity to `refine-code`, security-sensitive observations to
[security-review], and competing structural responses to [resolve-findings].
API/UI disagreement routes through [align] before implementation.
[suppressions]: {{package.suppressions_file}}
[security-review]: ../security-review/SKILL.md
[resolve-findings]: ../resolve-findings/SKILL.md
[align]: ../align/SKILL.md

Responsibility density is a lead, not a verdict:
`public functions × unique external imports × mixed-concerns flag`. Decompose
only when evidence shows a distinct caller set, test seam, reusable surface,
growth boundary, duplicated orchestration, or unstable callers.

### Module Deepening Lens

Use this lens when the hypothesis is that a module exposes nearly as much
complexity as it hides. A candidate passes the deletion test only when removing
or collapsing the shallow boundary would let one owner concentrate the behavior
behind a smaller stable interface; if it merely redistributes mechanics among
callers or moves lines, close the hypothesis as wrong.

A routed proposal names the domain concept, files and callers, observed friction,
proposed owner and seam, and the expected locality, leverage, and testability
gain. Add a compact before/after dependency sketch when it clarifies the claim.
Do not force a collapse or consolidation into `decompose-module`: route it to
`write-design-doc` when it changes ownership, interfaces, or caller contracts.
Speculative leads remain hypothesis outcomes rather than proposals, so a survey
may truthfully conclude that no deepening is warranted.

## Durable Report Contract

`DRIFT-PLAN.md` records scope, baseline, hypotheses, coverage, carried
calibration, adjustments, and resumable chunk status. `report.md` records the
ranked proposals, `confirmed / wrong / partial` hypothesis outcomes,
exceptions, collector limits, themes, durable facts, and routes; use current evidence.
Follow the [finding producer boundary]({{package.docs_root}}/guides/reference/reference-review-findings-format.md#finding-producer-boundary).
