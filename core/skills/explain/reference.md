# Explain — Progressive Reference

Load only the section needed for the selected output.

## Depth and Shape

- **Orientation** — intuition and takeaway for direction, not independent transfer.
- **Working understanding** — intuition, mechanism, and decision-changing caveats; default.
- **Deep transfer** — competing models, edge cases, sources, and expert nuance.

Choose concept/abstract, scientific mechanism, technical architecture, or
business/strategy structure from the reader's task, not from topic labels alone.

## Visual Decision

| Relationship | Smallest useful form |
| --- | --- |
| sequence or state change | flow or timeline |
| repeated exact mappings/comparisons | table |
| hierarchy, ownership, branching | tree |
| spatial UI arrangement | wireframe |
| already clear in prose | no visual |

## Output Shapes

- Concept/abstract: intuition → relationships → example → limits → takeaway.
- Scientific: observed effect → mechanism → evidence → uncertainty → implication.
- Technical: user goal → components/data flow → failure/authority boundaries → consequence.
- Business/strategy: decision → forces/options → trade-offs → recommendation → action.

## HTML Render Contract

Default to `premium-minimal-wide.html`; other templates in `templates/` are
style alternatives, not weaker validation modes. Preserve semantic landmarks,
one `<h1>`, keyboard-visible focus, readable line length, reduced-motion and
print styles, and authoritative accessible inline SVG for diagrams.

Mandatory before promotion:

- source fidelity: claims, citations, uncertainty, glossary, and takeaway match the outline;
- structure/accessibility: valid hierarchy, skip navigation, descriptive links,
  labels/captions, contrast, and usable content with JavaScript disabled;
- offline/security: no remote runtime assets, inline event handlers, `eval`, or
  unsanitized HTML sinks; dynamic behavior uses bounded `addEventListener` code;
- determinism/integrity: stable IDs, resolved local links/TOC, and byte-identical
  render for the same outline and date;
- portability: print rendering works and file size stays below 500 KB without
  embedded diagrams or 2 MB with them.

Validate a temporary artifact, then promote it. A mandatory failure blocks
promotion and is reported with its location. If browser accessibility/print
tooling is unavailable, run all source-level/offline checks, label the browser
gate degraded, and do not claim a full pass. Automated accessibility checks do
not replace a manual screen-reader limitation note.

## Briefing Variants

All variants use `operational-slate` and cite substantive claims to current
state. No source means no claim.

| Variant | Reader question | Core emphasis |
| --- | --- | --- |
| project | What is {{package.product_name}}, where is it going, and where are we? | strategy, initiatives, built vs vision |
| backlog | What was I doing and what is next? | in-flight, blocked, decisions, next action |
| pulse | Is anything on fire now? | stale work, lifecycle drift, merge health |
| strategy | How does the product fit together? | dependency map, vision gaps, rules |
| journey | What did the period produce? | milestones, inflections, lessons |

Use repository history/design docs plus authenticated GitHub state when the
variant needs it. If GitHub is unavailable, stop the live claim or produce an
explicitly repository-only partial brief. Surface lifecycle drift in findings;
never auto-move docs, labels, issues, or PR state from this skill.

`onboarding` remains the durable
[onboarding brief]({{package.docs_root}}/guides/dev-workflow/onboarding-brief.md), owned
by `design-handoff`, not a generated briefing variant.
