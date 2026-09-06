---
name: review
description: Independently assess a frozen patch against acceptance and local requirements, reporting actionable defects and their severity.
---

Read [the shared contract](../../CONTRACT.md). Confirm repository, base and
candidate identity and inspect the exact diff plus the relevant surrounding
code and acceptance evidence. Remain read-only. Report concrete defects with
severity, location, failure condition and practical impact; separate uncertainty
from demonstrated failure. Use the repository's severity and repair policy.

Name the reviewed revision, checks inspected or independently run, unresolved
findings and limits. If the candidate changes, distinguish applicable earlier
evidence from what needs repair verification. Missing diff/access/evidence is
an incomplete review, not a clean verdict. One independent review is required
for each real pilot change; no automatic second-model or cross-review layer.
