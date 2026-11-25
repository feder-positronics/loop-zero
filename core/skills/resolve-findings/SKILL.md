---
name: resolve-findings
description: Decide the response to each existing PR review finding: post factual rejections, hand code fixes to implementation; do not review.
---

# Resolve Findings

Read the PR acceptance, exact threads, affected code and current head. Verify
each finding's premise on that head; use
[staleness re-verification](reference.md#staleness-re-verification) as needed.
Follow the contract's [findings rule](../../CONTRACT.md#findings).

Fix valid blocking findings within commissioned scope or hand off bounded repairs
and validation through [response handoff](reference.md#response-handoff).
Explain repaired, inapplicable or unsupported findings in-thread with verified
evidence using `loopzero resolve`; never resolve silently. Suggestions are optional.
Resolving a thread sends GitHub no Actions event: after the last resolve, refresh
Eligibility through its workflow's manual PR-number input.
Requester-reserved, irreversible or acceptance/scope/cost changes and external
commitments require direction.

Do not manipulate severity; corrections need evidence in-thread, and critical or
security-path downgrades need requester authority. For conflicting findings,
reconcile their acceptance and impact. New critical evidence requires a separate
authorized PR review thread rather than silently expanding this batch.

After the delta, follow the contract's [round-two rule](../../CONTRACT.md#review-budget).
Return accepted repairs to delivery: fixing findings does not finish the task
until the fixed head is delivered.

Then continue the task you were given as far as it authorizes, or hand over
what is left of its acceptance. If two repairs have failed, the route is
probably wrong, so reconsider it before a third.
