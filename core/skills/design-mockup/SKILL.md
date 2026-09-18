---
name: design-mockup
description: Create a standalone HTML/CSS visual mockup to explore a UI direction before production implementation; use for product shape or aesthetic exploration, not production components.
---

# Design Mockup

Create one standalone HTML/CSS mockup that makes an intended interaction and
visual hierarchy inspectable while design changes are cheap. Keep it clearly
non-production and do not implement backend behavior.

## Grounding

Read the request, issue or `.loopzero/task.md`, relevant design artifact,
existing product surface, and [the mockup reference](reference.md). Identify the
actors, jobs, scenarios, consequential states, and design question the mockup
must answer.

Inspect existing component and token conventions when present. Reuse recognizable
patterns without treating current composition as a constraint against a better
direction. When multiple genuinely different directions help decide the design,
compare them; do not create variants merely to satisfy a process.

## Build And Inspect

- Save a self-contained HTML artifact at
  `docs/design/mockups/YYYY-MM-DD-feature-name.html`, preserving that convention
  unless the repository already documents an equivalent mockup location.
- Use semantic HTML, responsive behavior, accessible states, theme tokens, and
  realistic representative data.
- Demonstrate only the consequential states and interactions needed to judge
  the idea. Controls must work or be plainly marked non-functional.
- Do not simulate backend complexity or imply that the artifact is production.
- Inspect representative desktop and mobile widths, light and dark themes when
  relevant, keyboard flow, focus, contrast, and consequential interactions.
  Record any tooling or inspection limit that affects design judgment.

When the mockup is part of a loop-zero task, run `loopzero check` and put its
path plus material design assumptions in `.loopzero/task.md` Notes.

## Translation Handoff

Document which existing components and tokens map to the concept, which
production gaps remain, and which choices are visual assumptions rather than
settled architecture. Ask the stakeholder to validate material direction when
required; otherwise mark it assumed and vetoable.

## Exit

Exit when the core design decision is inspectable with realistic states, the
relevant viewport, theme, interaction, and accessibility conditions work,
controls are honest, translation gaps and assumptions are explicit, and the
artifact is unmistakably self-contained and non-production.
