# Explain Reference

Read only the section needed for the selected output.

## Depth And Shape

- **Orientation:** intuition and takeaway for direction, not independent transfer.
- **Working understanding:** intuition, mechanism, and decision-changing caveats;
  use this by default.
- **Deep transfer:** competing models, edge cases, sources, and expert nuance.

Useful shapes:

- concept: intuition, relationships, example, limits, takeaway;
- scientific: observed effect, mechanism, evidence, uncertainty, implication;
- technical: user goal, components and data flow, failure and authority
  boundaries, consequence;
- business: decision, forces and options, trade-offs, recommendation, action.

## Visual Choice

Use a flow or timeline for sequence, a table for repeated exact comparisons, a
tree for hierarchy or ownership, and a wireframe for spatial UI arrangement.
Use no visual when prose is already clearer.

## HTML Validation

Preserve semantic landmarks, one `h1`, visible keyboard focus, readable line
length, reduced-motion behavior, print styles, and accessible diagrams.

Before handoff verify:

- claims, citations, uncertainty, glossary, and takeaway match the outline;
- heading hierarchy, skip navigation, descriptive links, labels, captions, and
  contrast remain usable without JavaScript;
- there are no remote runtime assets, inline event handlers, `eval`, or
  unsanitized HTML sinks;
- local links and the table of contents resolve, IDs are stable, and repeat
  rendering is byte-identical for the same outline and date;
- print rendering works and the file stays below 500 KB without embedded
  diagrams or 2 MB with them.

Validate a temporary artifact, then promote it. A mandatory failure blocks
promotion and is reported with its location. If browser accessibility or print
tooling is unavailable, run the source-level and offline checks, label the
browser check unavailable, and do not claim a full pass.

## Briefing Variants

Every substantive claim needs current repository or authenticated GitHub
evidence. If GitHub is unavailable, omit live claims or produce an explicitly
repository-only partial briefing.

| Variant | Reader question | Emphasis |
| --- | --- | --- |
| project | What is this project, where is it going, and where is it now? | strategy, initiatives, built versus intended |
| backlog | What was in progress and what is next? | active, blocked, decisions, next action |
| pulse | Is anything currently unhealthy? | stale work, PR and merge health |
| strategy | How does the product fit together? | dependencies, gaps, governing choices |
| journey | What did the period produce? | milestones, inflections, lessons |

Report inconsistent issue, PR, or document state as drift; do not mutate it.
