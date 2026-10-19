---
name: grill-me
description: Stress-test a plan through one decision question per reply when asked to grill it or when consequential choices are coupled.
---

# Grill Me

Settle material branches of one plan without implementing it.

Every grilling reply, including the first, states a recommendation, the strongest
rejected alternative and the premise that makes the recommendation win, then
asks **exactly one short choice-based question**. Do not bundle questions.

Start from the supplied plan and settled requester decisions. Ground factual
premises in the issue, task and current code; state any premise you cannot
verify as an explicit assumption rather than withholding the question. For a high-impact, low-confidence, evidence-conflicted or genuinely
plausible alternative, make the deciding premise the question's focus.

Classify material branches:

- Evidence or an existing decision answers it: lock it with grounding.
- Reversible and explicitly delegated: recommend and assume it.
- Requester-reserved, outside delegated authority, or changing acceptance, scope,
  cost or external commitments: ask.
- Irrelevant to the outcome: defer with a reason.

“Your call” retains the recommendation as assumed. Do not reopen settled choices.

Give decisions stable short IDs and record status (`Locked`, `Assumed`,
`Deferred`), decision/evidence, strongest rejected alternative, deciding premise,
and implementation consequence or next owner. Keep issue-backed records in the
issue; after `loopzero start`, use `.loopzero/task.md` Notes for PR handoff.
Before exit place the complete record there. If neither home exists, ask which
issue/task owns it; do not create a tracker or call an unplaced record complete.

If a fact collapses the premise, stop and re-scope. If a question loops twice,
decide within scope or defer. On fatigue or “wrap up”, summarize and defer the
remainder immediately. Normal completion requires every material branch to have
a status and the requester to confirm the record once.
