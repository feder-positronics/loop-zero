---
name: refine-design-doc
description: Edit an existing design artifact until material findings are resolved or explicit blockers remain.
---

# Refine Design Doc

Make the artifact actionable for its intended next actor without implementing
the design. Use [review-design-doc](../review-design-doc/SKILL.md) for grounding
and review, and [shared guidance](../write-design-doc/reference.md) for decisions,
conventions, and validation. Preserve settled decisions unless evidence
invalidates their premise.

Repeat while material findings remain:

1. Review the current artifact for all critical and important gaps affecting
   its next action.
2. Resolve response-only findings against evidence and constraints here; use
   [resolve-findings](../resolve-findings/SKILL.md) only for existing PR threads.
   Ask only for requester-reserved choices or changes to acceptance, scope,
   cost, or external commitments.
3. Make focused edits, retaining source-of-truth links. Combine coupled findings
   when useful; suggestions do not become blockers.
4. Re-read and verify the underlying defect is resolved, beyond wording.

If the same important finding survives two passes, revisit the approach.
After five non-converging passes, report the surviving blocker.

Exit when no critical or important finding remains, decisions retain their
proper status, and owned open questions visibly block the relevant action.
Otherwise hand off the exact blocker without claiming readiness.
Record the artifact path and material decisions in task Notes when available.
