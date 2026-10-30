# Review Design Doc Reference

Use [the shared design contract](../write-design-doc/reference.md#design-contract)
and [decisions](../write-design-doc/reference.md#decisions) as the baseline,
at the depth needed for the artifact's next action.

## Need And Shape

Challenge evidenced need, product fit, reuse, and the strongest alternative.
Do not invent options. Check decision authority, reversibility, assumptions,
and owned open questions.

## Boundaries And Operation

Check state/data ownership, API and authorization boundaries, isolation,
lifecycle, migration, rollback, observability, and failures. Does the design
compose with consumers, UI, navigation, and domain vocabulary? Are capabilities
wired to consumers? Does temporary scaffolding have in-scope removal criteria?

## Implementation Readiness

Verify observable acceptance, credible validation, and costly-to-discover
integration/file touchpoints without prescribing ordinary coding steps.
Are material questions owned and visibly blocking? Does current code and
repository guidance support material premises?

Report layer crossing, unwired capability, speculative machinery, silent
temporary scaffolding, or contradiction with settled decisions when it
materially affects the goal.
