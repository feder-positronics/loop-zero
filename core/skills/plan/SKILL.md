---
name: plan
description: Bound a requested change with acceptance, ownership and validation before implementation when those decisions are not yet clear.
---

Read [the shared contract](../../CONTRACT.md) and the repository's local contract.
Produce a bounded real change: acceptance, affected paths, local constraints,
selected checks and current owner. Use the existing task/PR; a separate plan
file is optional. Resolve routine implementation choices from available evidence.

Stop dependent work when missing authority, conflicting ownership or a material
unresolved requirement would change the result. State the exact decision needed
and continue independent authorized preparation. Planning alone does not authorize
publication or merge beyond the original task and local policy.

Select all applicable deterministic gates before review, the validation child's
environment and Git-metadata isolation boundary, and one independent review
route with at most one bounded delta. Name the single PR evidence location and
consumer-owned `KNOWN-GAPS.md`; do not plan a finding ledger or mandatory phases.
