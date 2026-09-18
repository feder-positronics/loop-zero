# Review Design Doc Reference

Apply only checks that can change the artifact's intended next action.

## Need And Shape

- Is the need current and evidenced, with actors or jobs, intended outcome,
  non-goals, and a rejected simpler shape?
- Does the proposal reuse an existing surface before adding entities,
  endpoints, stores, or processes?
- Are materially different options represented, with the strongest rejected
  alternative and the premise that decides between them?
- Are decision authority, reversibility, assumptions, and open questions clear?

## Boundaries And Operation

- Are data and state ownership, API and authorization boundaries, isolation,
  lifecycle, migration, rollback, observability, and failure modes sufficient?
- Does the design compose with its consumers and existing UI, navigation, and
  domain vocabulary instead of creating a parallel model?
- Are capabilities actually wired to consumers rather than merely defined?
- Does temporary scaffolding have removal criteria inside the artifact's scope?

## Implementation Readiness

- Are acceptance criteria observable and validation routes credible?
- Are concrete integration or file touchpoints named where discovery would be
  costly, without prescribing ordinary implementation detail?
- Are unresolved material questions owned and visibly blocking the relevant
  next action?
- Do current code and repository guidance support every material premise?

Layer crossing, unwired capability, speculative new machinery, silent temporary
scaffolding, and contradiction with a settled decision are findings when they
materially affect the artifact's goal.
