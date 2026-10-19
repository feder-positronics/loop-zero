# Write Design Doc Reference

Apply at the depth appropriate to the artifact and next action.

## Grounding

Read the request, complete existing artifact, linked issue or task, upstream
decisions, repository guidance, and enough code to verify current-state claims,
including claimed component capabilities. Separate facts from assumptions.
Requester decisions stay settled unless evidence invalidates their premise;
assumptions remain vetoable; deferred decisions become open questions.

Repository changes follow [the delivery contract](../../CONTRACT.md) in the
task worktree.

## Artifact Choice

| Need | Artifact |
| --- | --- |
| Compare directions | Exploration |
| Lock an architecture or policy boundary | ADR |
| Coordinate delivery lanes | Initiative |
| Specify bounded implementation | Blueprint |
| Explain an existing procedure | Guide |

Use repository paths, templates, frontmatter, terminology, and labels.
Without a convention, choose and explain a descriptive Markdown path in the
documentation tree; avoid a new hierarchy for one document.
Link sources of truth instead of copying them.

## Design Contract

Cover the material:

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

Preserve `upstream:` or equivalent source relationships. Change generated
lifecycle fields and indexes through their source or generator.
For time-gated decisions, record review date, owner, evidence to inspect,
and reversal condition in the artifact; do not create reminder issues.
An implementation-ready ADR or blueprint needs converged material questions
or explicit blockers.

## Validate

Remove placeholders; verify links, frontmatter, current-state premises,
decision status, question owners, acceptance, and repository conventions.
Execute guides and report observed behavior; omit architecture analysis unless
the guide introduces a design decision.

## Type-Specific Checks

- Exploration: real alternatives and a decision route.
- ADR: durable boundary, consequences, reversal conditions.
- Initiative: delivery lanes, dependencies, owners, evidence.
- Blueprint: concrete paths, sequenced acceptance, risks, validation.
