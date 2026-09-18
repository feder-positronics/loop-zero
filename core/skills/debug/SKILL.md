---
name: debug
description: Diagnose broken behavior by ranking competing hypotheses and proving a root cause when the cause remains unclear after initial reproduction; for an understood failing test use fix-failing-tests.
---

# Debug

Produce a falsifiable root-cause explanation and the smallest evidence-backed
repair contract. Keep diagnosis separate from implementation.

## Reproduce And Test

1. Capture the exact scenario, environment, expected behavior, observed
   behavior, and smallest reliable reproduction.
2. Prefer a red-capable reproduction: an assertion, test, or command that exits
   nonzero for the defect and would turn green if the proposed cause were fixed.
   A log excerpt alone is supporting evidence, not the reproduction.
3. List plausible hypotheses in ranked order with the evidence for and against
   each. Choose the next experiment by how well it separates the leading
   explanations, then rerank after every result.
4. Confirm the cause by predicting and observing a result that distinguishes it
   from alternatives. A suspicious line or correlation is not proof.

Use synthetic or local data first. Redact credentials, tokens, database URLs,
personal data, and user content before persisting or quoting evidence. Add
temporary instrumentation only when authorized and necessary; record it, avoid
secret-bearing output, and remove it before exit. Do not edit production code,
restart remote services, or mutate remote data without explicit authority.
Any production-derived experiment must be explicitly in scope and name its
cleanup or recovery contract before it runs.

## Exit

Return the reproduction, ranked and rejected hypotheses, discriminating
evidence, confirmed cause, blast radius, smallest repair and validation
contract, and remaining uncertainty. If proof is impossible, name the exact
missing evidence or authority. Stop before implementation and route an
understood test repair to [fix-failing-tests](../fix-failing-tests/SKILL.md) or
other code changes to [implement](../implement/SKILL.md).
