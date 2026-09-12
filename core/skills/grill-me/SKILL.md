---
name: grill-me
description: >-
  Stress-test a plan or design until material decisions are settled, assumed, or
  deferred, producing a durable Decision Record. Use when the user asks to be
  grilled or when ambiguity is structurally coupled. For routine pre-execution
  alignment use `align`.
---

# Grill Me

Use owner attention only for decisions that genuinely need owner judgment.

## Grounding

Read the plan, relevant implementation,
[owner doctrine]({{package.docs_root}}/design/owner-doctrine.md),
[users and jobs]({{package.docs_root}}/design/users-and-jobs.md), and
[open questions]({{package.docs_root}}/design/open-questions.md). Verify factual
premises in the repository. Existing owner decisions enter the record directly;
do not turn them back into questions.

## Method

Build the material decision set, then classify each branch:

- doctrine/evidence answers it → decide and cite the grounding;
- reversible and inside earned authority → decide, log, and continue;
- Reserved or outside authority → ask;
- irrelevant to the current outcome → Deferred with a reason.

Before every owner question, commit to a recommendation, the strongest
rejected alternative, and the premise that makes the recommendation win. Ask
one short choice-based question at a time. If the owner says “your call,” keep
the recommendation as Assumed and continue.

High-weight, low-confidence, doctrine-conflicted, or genuinely plausible
alternative decisions use propose-and-challenge before the recommendation.

## Decision Record

Use the
[canonical Decision Record contract]({{package.docs_root}}/guides/reference/reference-review-findings-format.md#decision-record-schema).
It owns the Locked/Assumed/Deferred states, stable identifiers and anchors,
calibration envelope, durable routing, and decision/verdict/outcome telemetry.
Each material entry retains its `id` and `<!-- decision id=... -->` anchor and
emits `agent_event.py decision` as that contract requires. An un-routed record
is lost.

## Stop Conditions

- A discovered fact collapses the premise → stop and re-scope.
- The same question loops twice → decide within authority or Deferred; do not
  rephrase indefinitely.
- Owner fatigue or “wrap up” → summarize immediately and defer the remainder.

## Exit Criteria

- [ ] Every material branch is Locked, Assumed, or Deferred.
- [ ] Doctrine/authority answered questions were not re-asked.
- [ ] Owner questions carried a recommendation and strongest alternative.
- [ ] The owner confirmed the record once; no repeated ceremonial confirmation.
- [ ] The complete record has a durable home and telemetry.

## Related

- `align` · `resolve-findings` · `write-design-doc` · `refine-design-doc`
