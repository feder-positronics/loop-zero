---
name: resolve-findings
description: Decide the response to each existing PR review finding: post factual rejections, hand code fixes to implementation; do not review.
---

# Resolve Findings

Read the PR acceptance, exact threads, affected code and current head. Verify
each finding's premise on that head; use
[staleness re-verification](reference.md#staleness-re-verification) as needed.
Findings stay in PR threads, never issues, backlogs or tracking files.

Fix valid blocking findings within commissioned scope or hand off bounded repairs
and validation through [response handoff](reference.md#response-handoff).
Explain repaired, inapplicable or unsupported findings in-thread with verified
evidence using `loopzero resolve`; never resolve silently. Suggestions are optional.
Requester-reserved, irreversible or acceptance/scope/cost changes and external
commitments require direction.

Do not manipulate severity; corrections need evidence in-thread, and critical or
security-path downgrades need requester authority. For conflicting findings,
reconcile their acceptance and impact. New critical evidence requires a separate
authorized PR review thread rather than silently expanding this batch.

When a second review flags the same file or defect class, audit every producer,
consumer, caller, reader and transition of that class before the next repair.
After three `request_changes` primaries on one PR, split it or return to
[plan](../plan/SKILL.md) and tell the requester why. Follow the contract's
[review budget and lineage-reset rule](../../CONTRACT.md#review-budget).
Return accepted repairs to delivery; bounded fixes alone do not complete the
commissioned outcome.

After this step, resume the commissioned outcome within the authorized finish,
or hand over its remaining acceptance; after two unsuccessful repairs,
reconsider the route.
