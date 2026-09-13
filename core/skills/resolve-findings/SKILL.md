---
name: resolve-findings
description: >-
  Turn existing review, audit, or design findings into concrete decisions and a
  durable Decision Record. Use when findings already exist and competing
  responses need selection. For a new proposal without findings use `grill-me`.
---

# Resolve Findings

Resolve each finding at the lowest interruption cost consistent with sound
judgment and {{skill_tokens.doctrine_d18}} authority.

## Inputs And Grounding

Read the source finding, affected artifact/code, acceptance intent,
[owner doctrine]({{package.docs_root}}/design/owner-doctrine.md), and matching
[open-question]({{package.docs_root}}/design/open-questions.md) entries. Apply
[staleness re-verification](reference.md) to saved audit or moved-diff findings.
Respect {{skill_tokens.doctrine_d14}}’s owner-attention budget. A doctrine-answered case is decided, not
escalated; Reserved matters remain owner calls under
[design.mdc]({{package.rules_root}}/design.mdc), without a local variant.

When adjudication owns exact Finding Ledger IDs across more than one step,
acquire one `evidence-only` finding lease for those IDs before a
`resolution-adjudication` review dispatch, per the finding-lease contract in
[`parallel-agents.mdc`]({{package.rules_root}}/parallel-agents.mdc).
Findings arrive here via the routing predicate in
[`review-gate.mdc`]({{package.rules_root}}/review-gate.mdc) Flow step 3, or as
source-based routes from audits and campaigns.

Adjudication is closed-world. A governed `resolution-adjudication` result maps
each supplied ID to exactly one `accept`, `narrow`, `merge`, or `reject`
disposition and emits no findings, risks, or recommended follow-ups. Genuinely
new critical evidence stops through `escalation_reason`; it is never deposited
from inside disposition. Route any later discovery as a separate authorized
review instead of recursively growing this batch.

## Decision Method

For each finding, state the decision and acceptance condition, then supply its
category, weight, reversibility, confidence, doctrine/evidence, strongest
rejected alternative, and expected outcome. Apply category authority:

- non-reserved findings default to {{skill_tokens.doctrine_d18}} agent-decided: decide now, record the
  Decision Record, and proceed;
- Reserved/irreversible/outward/costly/scope-expanding → escalate to `grill-me`.

High-weight, low-confidence, doctrine-conflicted, or plausible-alternative
autonomous calls use propose-and-challenge against the deciding premise. Route
the chosen response to the narrowest implementation or documentation workflow;
this skill records decisions but does not apply them.

Severity corrections and `same_defect_as` lineage are explicit decisions, not
counter-management shortcuts. Preserve original severity and every tree-scoped
ID. Important corrections require this skill's durable Decision Record;
critical or security-path corrections require owner authority. Append lineage
only with accepted reviewer confirmation, never text similarity.

Low-stakes mechanical cases need one rationale, not an option essay. For
high-impact cases, compare only materially different options and recommend the
cleanest long-term architecture.

## Decision Record

Use the
[canonical Decision Record contract]({{package.docs_root}}/guides/reference/reference-review-findings-format.md#decision-record-schema).
It owns Locked/Assumed/Deferred, the stable `id` and
`<!-- decision id=... status=... -->` anchor, calibration metadata, durable
routing, scheduled-review template, and decision/verdict/outcome telemetry.
Emit `agent_event.py decision` for each material call. A settled later-evidence
check uses `review=scheduled deadline=YYYY-MM-DD`; an un-routed record is lost.

## Failure Handling

- All options rejected → treat the rejection as a new constraint, regenerate
  once, then Deferred if still unresolved.
- Two findings conflict → resolve the higher-impact one and re-triage the
  other.
- Post-decision hatch — a **resolved decision** fails two **implementation
  attempts** → re-enter this skill and select a different recorded option; if
  no viable option remains, escalate to the owner. Do not grind the same fix.
  (Distinct from the Review Gate's 2-review-pass hatch, which routes findings
  here in the first place; the two counters never mix.)

## Exit Criteria

- [ ] Every finding is Locked, Assumed, or Deferred with a stable id.
- [ ] Acceptance condition, grounding, rejected alternative, and rationale exist.
- [ ] Autonomous calls carry authority/calibration metadata and challenge when required.
- [ ] Every response has a durable route; this skill implemented none of them.
- [ ] Settled time-gated reviews use the canonical scheduled template and date.
- [ ] Reserved/owner-only calls were not inferred.
- [ ] Owned finding leases have a structured handoff and explicit release,
      implementation settlement, or audited owner-gone recovery.

## Routing

- backend/frontend behavior → `{{skill_routes.implement_backend.name}}` / `{{skill_routes.implement_frontend.name}}`
- UI defect → `{{skill_routes.fix_ui_bug.name}}`; tests → `write-tests` / `fix-failing-tests`
- no-behavior cleanup → `refine-code`; structural split → `decompose-module`
- design artifact → `write-design-doc` / `refine-design-doc`
