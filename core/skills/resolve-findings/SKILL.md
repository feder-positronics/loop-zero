---
name: resolve-findings
description: Decide the response to each existing PR review finding: post factual rejections, hand code fixes to implementation; do not review.
---

# Resolve Findings

Read PR objective/acceptance, exact threads, affected code and current head.
Use [staleness re-verification](reference.md#staleness-re-verification) when
the head moved. Findings stay in PR threads, never issues, backlogs or tracking files.

## Decide each thread

- State failure and acceptance conditions; verify applicability on current head.
- Valid `critical`/`important`: hand off the narrowest fix and validation.
- Already fixed, inapplicable or unsupported: **verify on current head, then
  post the reply yourself with `loopzero resolve <id> "<evidence-backed reason>"`**.
  Replying is not implementation.
- Suggestions are optional; do not expand scope for them.
- Requester-reserved, irreversible, acceptance/scope/cost changes or external
  commitments need requester direction; never infer it.

Give each supplied thread one status: bounded fix, factual rejection,
reviewer-confirmed duplicate, deferred suggestion or requester decision.
Hand fixes to the narrowest implementation or documentation step using
[response handoff](reference.md#response-handoff); that step replies with the
fix commit after applying it.

Preserve severity unless corrected with evidence in the thread. Critical or
security-path downgrades need requester authority; identifying findings as the
same defect needs reviewer confirmation there. For conflicting findings,
satisfy higher-impact acceptance and re-evaluate the other. After a response
fails twice, choose another evidence-backed route or ask if none remains.

Do not add findings to the batch. Genuinely new critical evidence stops
adjudication and requires a separate authorized PR review thread.

## Repeated findings and review budget

Follow the [review budget](../../CONTRACT.md#review-budget).
A second review flagging the same file or defect class requires a `deep`
profile audit of the whole class—every producer/consumer, caller, reader and
transition—then one repair, not individual patches.

**A delta requesting changes leaves no further delta.** After fixes, squash/amend
so **neither reviewed commit remains an ancestor of HEAD**, then request a new
primary. A fresh lineage resets budget, not the problem or the count: after
three `request_changes` primaries on one PR, split or return to `plan` and
explain why to the requester.

Exit when every supplied open blocking thread has a bounded fix with validation or
reasoned reply, with no inferred requester authority. Threads remain the record.
