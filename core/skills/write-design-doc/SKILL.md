---
name: write-design-doc
description: >-
  Research and write an exploration, ADR, initiative, blueprint, or guide for a
  feature, architecture, or workflow change. Use when implementation needs a
  durable design contract. For reviewing an existing artifact use
  `review-design-doc`.
---

# Write Design Doc

Produce the smallest durable artifact that lets the next actor proceed without
re-deriving intent or architecture. The agent chooses its research path; the
contract below defines what the result must prove.

## Before Writing

- Align material ambiguity and follow the
  [worktree rule]({{package.rules_root}}/parallel-agents.mdc) when standalone.
- Run `{{toolchain.commands.orient}}`; read [Vision]({{package.docs_root}}/design/vision.md),
  [owner doctrine]({{package.docs_root}}/design/owner-doctrine.md),
  [users/jobs]({{package.docs_root}}/design/users-and-jobs.md),
  [open questions]({{package.docs_root}}/design/open-questions.md),
  [owner inbox]({{package.docs_root}}/design/owner-inbox.md), and overlapping artifacts.
- Verify every “reuse X” or “system Y provides Z” premise against current code.
- Consume existing Decision Records: Locked is settled, Assumed remains
  vetoable, Deferred becomes an open question.

Skip the artifact entirely when a direct prompt is clear, small, and safely
implementable in one bounded change.

## Artifact Choice

| Need | Artifact |
|---|---|
| Investigate or compare directions | `{{package.docs_dir}}/design/explorations/YYYY-MM-DD-*.md` |
| Lock a durable architecture/policy boundary | `{{package.docs_dir}}/architecture/adrs/YYYY-MM-DD-*.md` |
| Coordinate multiple delivery lanes | `{{package.docs_dir}}/design/initiatives/active/*/index.md` |
| Specify bounded implementation | `{{package.docs_dir}}/design/blueprints/planned/YYYY-MM-DD-*.md` |
| Explain an existing procedure | guide track under `{{package.docs_dir}}/guides/` |

Use the [design-doc reference]({{package.docs_root}}/guides/reference/design-doc-reference.md)
and matching template; link SSoTs rather than copying them.

## Design Contract

The artifact must contain, at the depth appropriate to its type:

- now-trigger, actors/jobs, intended outcome, non-goals, and success evidence;
- verified current state, data/state boundaries, consumers, integration points,
  failure modes, and blast radius;
- materially different options, chosen shape, strongest rejected alternative,
  and why the deciding premise wins;
- simplest-shape/reuse audit and composition with existing product surfaces;
- domain terms and user-facing labels reuse the [Vision glossary] and relevant
  `{{package.docs_dir}}/domain/` vocabulary, or explicitly record the intentional divergence;
- implementation boundary, concrete acceptance/validation, rollback, and
  observability where relevant;
- open questions routed to `open-questions.md`, never left only in chat.

For user-facing work, scenarios precede production UI and material UI shape is
validated with a mockup or existing pattern. For implementation blueprints,
name concrete paths and phased acceptance without prescribing inferable coding
steps.

For unresolved non-visual logic or state uncertainty, an exploration may use
the bounded [Executable Spike Track].

## Decision Record

Use the
[canonical Decision Record contract]({{package.docs_root}}/guides/reference/reference-review-findings-format.md#decision-record-schema).
It owns Locked / Assumed / Deferred, stable `id` values,
`<!-- decision id=... status=... -->` anchors, D-18 calibration, scheduled
reviews, durable routing, and telemetry. Emit `agent_event.py decision` for each
new material call; an un-routed Decision Record is lost. High-weight,
low-confidence, doctrine-conflicted, or plausible-alternative autonomous calls
use propose-and-challenge before documentation.

## Tracking And Validation

- Set valid frontmatter and `upstream:` links; never hand-edit generated
  blueprint lifecycle state or indexes.
- Log a stable proposal ID with `agent_event.py proposal` when creating an
  exploration, ADR, initiative, or blueprint.
- Run `{{toolchain.commands.docs_verify}}`; for implementation-ready ADRs/blueprints route to
  `refine-design-doc` when convergence is warranted.

Guide track: execute the documented procedure, write observed reality, and skip
architecture/option work unless the guide introduces a design decision. All
applicable exit criteria still apply: a guide needs verified procedure evidence,
actionable validation, valid frontmatter, SSoT links, docs checks and no
placeholders. Time-gated reviews, if any, still use the scheduled template;
design alternatives and consumer/composition contracts apply when their
subjects are part of the guide.

## Exit Criteria

- [ ] Correct artifact/type exists with valid frontmatter and SSoT links.
- [ ] Premises/current state are verified; actors, now-trigger, and simplest
  shape are explicit.
- [ ] Decisions, rejected alternative, open questions, and authority are durable.
- [ ] Time-gated reviews, if any, use the canonical scheduled template.
- [ ] Acceptance/validation and consumer/composition contracts are actionable.
- [ ] Tracking/docs checks pass and no placeholder content remains.

## Related

- `grill-me` · `review-design-doc` · `refine-design-doc` · `design-handoff`

[Vision glossary]: {{package.docs_root}}/design/vision.md#glossary
[Executable Spike Track]: {{package.docs_root}}/guides/reference/design-doc-reference.md#executable-spike-track
