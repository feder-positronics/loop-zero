---
name: grill-me
description: Stress-test a plan or design by settling coupled material decisions before implementation; use when the requester asks to be grilled or several consequential choices depend on one another.
---

# Grill Me

Stress-test one plan or design until every material branch is settled, assumed,
or explicitly deferred. Do not implement the result.

## Grounding

Read the plan, relevant issue or `.loopzero/task.md`, and enough current code to
verify factual premises. Treat decisions already made by the requester as
settled; do not turn them back into questions.

## Method

Build the material decision set, then classify each branch:

- evidence or an existing decision answers it: lock it and cite the grounding;
- reversible and inside explicitly delegated authority: recommend it and
  continue as assumed;
- reserved by the requester or outside delegated authority: ask, even when the
  choice is reversible and otherwise inside the stated scope;
- changes acceptance, scope, cost, or an external commitment: ask;
- irrelevant to the current outcome: defer it with a reason.

Before each question, state the recommendation, strongest rejected alternative,
and premise that makes the recommendation win. Ask one short choice-based
question at a time. If the requester says "your call," retain the recommendation
as assumed and continue.

For a high-impact, low-confidence, evidence-conflicted, or genuinely plausible
alternative, challenge the deciding premise before making the recommendation.

## Decision Record

Give every material decision a stable short ID and record:

- status: `Locked`, `Assumed`, or `Deferred`;
- the decision and evidence;
- strongest rejected alternative and deciding premise;
- the implementation consequence or next owner.

For issue-backed work, keep the record in the issue. After `loopzero start`, put
it in `.loopzero/task.md` Notes so `loopzero pr` carries it into the PR body.
Before exit, place the complete record in one of those homes. If neither exists,
stop and ask which issue or task should own it; do not create a separate tracker
or treat an unplaced record as complete.

## Stop And Exit

- If a discovered fact collapses the premise, stop and re-scope.
- If the same question loops twice, decide within scope or defer it.
- If the requester shows fatigue or says "wrap up," summarize immediately and
  defer the remainder.
- Exit when every material branch has a status and the requester has confirmed
  the record once. Hand the record to the issue or task Notes for planning and
  implementation.
