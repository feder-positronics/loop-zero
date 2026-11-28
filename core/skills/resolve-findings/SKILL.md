---
name: resolve-findings
description: Verify existing PR findings, fix commissioned blockers or hand off repairs, and explain each disposition in-thread.
---

# Resolve Findings

Read PR acceptance, exact threads, affected code, reviewed SHA and current head.
Test each premise at current head with a minimal reproducer or acceptance evidence.
Classify as reproducible, fixed by a named commit or unsupported.

Fix commissioned blockers or use [response handoff](reference.md#response-handoff).
Follow [contract resolution](../../CONTRACT.md#blockers) with the fix commit or
evidence-backed reason. Resolve factual rejections now and fixes after proof at
the pushed head, before readiness. Post only the disposition for security
defects. For outdated acceptance, verify authorized criteria at the current
head and resolve with that evidence. Seek direction for requester-reserved or
irreversible changes, acceptance/scope/cost changes or external commitments.

Severity corrections require in-thread evidence. Critical or security-path
downgrades need requester authority. Reconcile conflicting findings' acceptance
and impact. Report new critical evidence to the requester. Fix new critical
defects in this batch or file a follow-up for non-security defects. For security
defects use [security-review](../security-review/SKILL.md) and send evidence and
follow-up privately to the requester.

Before delta, compare the full repair to the primary-reviewed head. Identify
affected callers and guarantees. Use existing test seams to exercise the
failure and affected success, failure or timing behavior. Apply [security-review](../security-review/SKILL.md) to changed trust boundaries. Record evidence, run existing checks and follow
the [review budget](../../CONTRACT.md#review-budget) through delivery or handoff.
