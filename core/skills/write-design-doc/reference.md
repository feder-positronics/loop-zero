# Write Design Doc Reference

Read this reference when selecting and drafting a design artifact.

## Artifact Choice

| Need | Artifact |
| --- | --- |
| Investigate or compare directions | Exploration |
| Lock a durable architecture or policy boundary | ADR |
| Coordinate multiple delivery lanes | Initiative |
| Specify bounded implementation | Blueprint |
| Explain an existing procedure | Guide |

Prefer the repository's established path and template for the chosen type. If it
has none, use a descriptive Markdown filename in its documentation tree rather
than introducing a new hierarchy for one document.

## Design Contract

At the depth appropriate to the artifact, include:

- current trigger, actors or jobs, intended outcome, non-goals, and success
  evidence;
- verified current state, data and state boundaries, consumers, integration
  points, failure modes, and blast radius;
- materially different options, the chosen shape, strongest rejected
  alternative, and the premise that decides between them;
- a simplest-shape and reuse audit, including composition with existing product
  surfaces;
- repository domain terms and user-facing labels, with intentional divergence
  called out;
- implementation boundary, concrete acceptance and validation, rollback, and
  observability where relevant;
- open questions, their owners, and the work each question blocks.

For unresolved non-visual logic or state uncertainty, an exploration may define
a bounded executable spike. State the question, time or scope bound, throwaway
or retained output, and evidence that ends the spike.

## Decisions

Record each material decision with a stable short ID, its status (`Locked`,
`Assumed`, or `Deferred`), evidence, strongest rejected alternative, deciding
premise, and implementation consequence. High-impact, low-confidence,
evidence-conflicted, or plausible-alternative decisions need an explicit
challenge to the deciding premise.

## Artifact Lifecycle

- Preserve an artifact's `upstream:` links or equivalent relationship to the
  source issue, exploration, ADR, or initiative.
- Do not hand-edit generated lifecycle fields or indexes; change their source or
  use the repository's generator.
- For a time-gated decision, record the review date, owner, evidence to inspect,
  and condition that would change the decision. Do not create a reminder issue.
- When an ADR or blueprint is meant to be implementation-ready, revise and
  revalidate it until material questions converge or are explicit blockers.

## Type-Specific Checks

- Exploration: compares real alternatives and ends with a decision route.
- ADR: states the durable boundary, consequences, and reversal conditions.
- Initiative: separates delivery lanes, dependencies, ownership, and evidence.
- Blueprint: names concrete paths, sequenced acceptance, risks, and validation.
- Guide: execute the procedure, report observed reality, and omit architecture
  analysis unless the guide itself introduces a design decision.
