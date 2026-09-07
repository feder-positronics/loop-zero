---
name: review
description: Independently assess a frozen patch against acceptance and local requirements, reporting actionable defects and their severity.
---

Read [the shared contract](../../CONTRACT.md). Confirm repository, base and
candidate identity and inspect the exact diff plus the relevant surrounding
code and acceptance evidence. Remain read-only. Report concrete defects with
severity, location, failure condition and practical impact; separate uncertainty
from demonstrated failure. Use the repository's severity and repair policy.

Classify merge-blocking defects as critical or important; require resolution or
an explicit waiver with rationale by merge authority in this PR. Suggestions
stay in PR comments, are never counted, and are never exported as durable debt.
Verifier/trust checks report pass/fail, not review findings. Findings expire at
merge; only deliberate closeout curation can add a concrete known gap.

Name the exact reviewed commit, inspected gates and limits in the single PR
body evidence block. Missing diff/access/evidence is incomplete, not a clean
verdict. Perform one independent review per PR, with at most one bounded delta
for substantive repairs. Mechanical fixes need only deterministic gates rerun;
record their delta from the reviewed commit without claiming it was reviewed.
An unavailable harness must not prevent using another independent delta route.

Monthly, sample merged reviews and observed defects: distinguish actionable real
defects from noise and missed defects, then propose a focused skill adjustment
in a calibration PR. Do not preserve a finding archive or count suggestions.
