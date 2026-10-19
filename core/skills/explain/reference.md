# Explain Reference

## HTML Validation

Create a self-contained, offline-safe, accessible, printable artifact with
semantic landmarks, one `h1`, visible focus, readable line length, reduced motion,
print styles and accessible diagrams.

Verify before handoff:

- Claims, citations, uncertainty, glossary and takeaway match the outline.
- Heading hierarchy, skip navigation, descriptive links, labels, captions and
  contrast work without JavaScript.
- No remote runtime assets, inline event handlers, `eval` or unsanitized HTML sinks.
- Local links and contents resolve, IDs stay stable, and identical outline/date
  inputs render byte-identically.
- Print rendering works; size is below 500 KB without embedded diagrams or
  2 MB with them.

Validate a temporary artifact before promoting it to the requested or established
explainer path. Every available mandatory check must pass; report a failure's
location and block promotion. If browser accessibility or print tooling is
unavailable, run source/offline checks and report unavailable checks without
claiming a full pass.

## Briefing Variants

Support every substantive claim with current repository or authenticated GitHub
evidence. Without GitHub, omit live claims or label the briefing repository-only
and partial. Report inconsistent issue, PR or document state as drift; do not
repair it.

| Variant | Reader question | Emphasis |
| --- | --- | --- |
| project | What is this project, its direction and current state? | strategy, initiatives, built versus intended |
| backlog | What is in progress and next? | active, blocked, decisions, next action |
| pulse | Is anything unhealthy? | stale work, PR and merge health |
| strategy | How does the product fit together? | dependencies, gaps, governing choices |
| journey | What did the period produce? | milestones, inflections, lessons |
