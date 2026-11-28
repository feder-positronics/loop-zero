# Write Design Doc Reference

Apply at the depth appropriate to the artifact and next action.

## Grounding

In the task worktree, read the request, full artifact, linked issue/task,
upstream decisions, repo guidance and code that verifies current capabilities.
Keep requester decisions settled unless evidence defeats their premise. Mark
assumptions as vetoable and keep deferred decisions open.

## Artifact Choice

| Need | Artifact |
| --- | --- |
| Compare directions | Exploration |
| Lock an architecture or policy boundary | ADR |
| Coordinate delivery lanes | Initiative |
| Specify bounded implementation | Blueprint |
| Explain an existing procedure | Guide |

Follow repo path, template, frontmatter, term and label conventions. Otherwise
explain a descriptive Markdown path in the docs tree. Avoid a new hierarchy for
one file. Link to the owning rule for the same actor instead of copying it.

## Design Contract

Cover the material:

- Plain-language owner opening and shape-matched visuals per
  [Reporting](../../CONTRACT.md#reporting).
- Trigger, actors/jobs, outcome, non-goals, success evidence.
- Verified current state, state/data boundaries, consumers, integration,
  failures, and blast radius.
- Material alternatives and chosen shape; prefer the simplest shape and reuse
  existing surfaces.
- Domain terms and user-facing labels; explain intentional divergence.
- Implementation boundary, observable acceptance, validation, rollback,
  and observability where relevant.
- Open questions, owners, and the work each blocks.

A bounded executable spike may resolve non-visual logic or state uncertainty:
name the question, time/scope bound, retained or throwaway output, and evidence
that ends it.

## Decisions

Record material decisions with stable short IDs, status (`Locked`, `Assumed`,
`Deferred`), evidence, strongest rejected alternative, deciding premise, and
implementation consequence. Challenge the premise for high-impact,
low-confidence, evidence-conflicted decisions or plausible alternatives.

## Artifact Lifecycle

Preserve recorded source relationships. Change generated
lifecycle fields and indexes through their source or generator.
For time-gated decisions, record review date, owner, evidence to inspect,
and reversal condition in the artifact; do not create reminder issues.
An implementation-ready ADR or blueprint needs converged material questions
or explicit blockers.

## Validate

Remove placeholders; verify [Reporting](../../CONTRACT.md#reporting) compliance,
links, frontmatter, current-state premises, decision status, question owners,
acceptance, and repository conventions.
Execute guides and report observed behavior; omit architecture analysis unless
the guide introduces a design decision.

## Type-Specific Checks

- Exploration: real alternatives and a decision route.
- ADR: durable boundary, consequences, reversal conditions.
- Initiative: delivery lanes, dependencies, owners, evidence.
- Blueprint: concrete paths, sequenced acceptance, risks, validation.
