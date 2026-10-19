---
name: resolve-findings
description: Decide the response to existing GitHub PR review findings and hand a bounded fix or reasoned thread resolution to the next step; do not perform the review or implement the response.
---

# Resolve Findings

Resolve existing PR review threads into concrete, evidence-backed responses.
This skill decides and routes the response; it does not edit code.

## Grounding

Read the PR objective and acceptance, the exact review thread, affected code,
and current head. Use [staleness re-verification](reference.md) whenever the head
has moved since the comment. Findings live only in PR review threads: do not copy
them into an issue, backlog, or local tracking file.

## Decide Each Thread

1. Restate the demonstrated failure condition and acceptance condition.
2. Verify whether it still applies on the current head.
3. Choose the narrowest response:
   - a valid `critical` or `important` finding gets a bounded fix and validation;
   - an already-fixed, inapplicable, or unsupported finding gets a factual reply
     explaining why the thread can be resolved;
   - a `suggestion` is optional and must not expand the task merely to address it.
4. Ask the requester only when the response is requester-reserved,
   irreversible, or changes acceptance, scope, cost, or an external commitment;
   never infer that authority.
5. Hand the chosen response to the narrowest implementation or documentation
   step. Preserve severity unless the thread itself records the correction and
   evidence.

Give each supplied thread exactly one status: bounded fix, factual rejection,
reviewer-confirmed duplicate, deferred suggestion, or requester decision. Do not
grow the batch with new findings. Genuinely new critical evidence stops this
adjudication and must be raised in a separate authorized PR review thread.

Do not downgrade a critical or security-path finding without requester
authority, or mark findings as the same defect without reviewer confirmation in
the thread. When two findings conflict, satisfy the higher-impact acceptance
requirement and re-evaluate the other. If a chosen response fails twice, select
a different evidence-backed route; ask the requester if none remains.

## Repeated Findings

Repairs follow the [contract's review budget](../../CONTRACT.md#review-budget).
A fresh lineage resets the budget, not the problem. When a second review flags
the same file or defect class again, stop patching single findings: audit the
whole class (every producer and consumer: caller, reader, transition) with the
`deep` profile, fix it in one repair, and only then request review. After three
`request_changes` primaries on one PR, split the PR or return to `plan`; tell
the requester why. Squashing or rewriting commits to obtain another primary
does not reset that count.

## Exit

Exit when every supplied open blocking thread has either a bounded fix with
validation or a reasoned thread reply, and no requester-only decision is
inferred. Hand the fix list and proposed replies to implementation; after the
response is applied, the PR review threads remain the authoritative record.
