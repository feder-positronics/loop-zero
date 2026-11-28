---
name: resolve-findings
description: Verify existing PR findings, fix commissioned blockers or hand off repairs, and explain each disposition in-thread.
---

# Resolve Findings

Read the PR acceptance, exact threads, affected code, reviewed SHA and current
head. Reopen each premise on the current head using the smallest reproduction
or current acceptance evidence. Classify it as reproducible, fixed by a named
commit or unsupported. Follow the contract's [findings rule](../../CONTRACT.md#findings).

Fix valid blocking findings within commissioned scope or hand off bounded repairs
and validation through [response handoff](reference.md#response-handoff).
Use `loopzero resolve <id> "<fix commit or evidence-backed reason>"` in-thread:
resolve factual rejections now and fixes after verification on the pushed head,
before readiness and merge. Post only the disposition for security defects.
If a finding cites outdated acceptance, verify the authorized criteria against
the current head and resolve the mistaken premise with that evidence.
Requester-reserved, irreversible or acceptance/scope/cost changes and external
commitments require direction.

Do not manipulate severity; corrections need evidence in-thread, and critical or
security-path downgrades need requester authority. For conflicting findings,
reconcile their acceptance and impact. Report new critical evidence to the requester.
Fix new critical defects in this batch or file a follow-up issue for non-security
defects. For security defects, follow [security-review](../security-review/SKILL.md)
and send evidence and any follow-up privately to the requester.

Before requesting the delta, compare the full repair with the primary-reviewed
head. Identify affected callers and guarantees for each repaired behavior. Use
existing test seams to exercise the failing case and any success, failure or
timing behavior the repair can change. Apply [security-review](../security-review/SKILL.md)
to changed trust boundaries. Record the evidence with the repair, run the
existing checks and follow the contract's [review budget](../../CONTRACT.md#review-budget)
through delivery or handoff of remaining acceptance.
