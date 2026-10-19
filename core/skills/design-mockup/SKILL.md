---
name: design-mockup
description: Explore a UI direction with a standalone HTML/CSS mockup before production implementation.
---

# Design Mockup

Make an interaction and visual hierarchy inspectable while changes are cheap.
Keep the artifact clearly non-production; omit backend implementation.

Read the request, issue/task, relevant design and existing surface. Identify
actors, jobs, scenarios, consequential states and the design question. Inspect
available component catalogs, tokens, theme/layout guidance, prior mockups and
browser-testing instructions. Otherwise infer only from production UI and state
the limit. Reuse recognizable patterns while allowing better composition;
compare directions only when they help decide.

Save at the documented mockup location or
`docs/design/mockups/YYYY-MM-DD-feature-name.html`. Use semantic, responsive HTML,
accessible states, theme tokens and realistic data exposing relevant hard cases:
long labels, density, truncation, empty/loading/error states, permissions or
destructive actions.

Keep CSS and bounded interaction JavaScript inline, title the page as a mockup,
and avoid remote runtime dependencies, production imports or an application-server
requirement. Demonstrate consequential transitions, hierarchy and feedback.
Controls must work or be plainly non-functional; use static alternatives when
interaction does not inform the design question.

Inspect representative desktop and mobile widths, keyboard order/focus,
headings/landmarks, control names, contrast, reduced motion, overflow, zoom and
both themes when supported. Report inspected conditions and unavailable
browser-dependent checks.

Document mappings to existing components/tokens, production gaps and visual
assumptions distinct from settled architecture. Seek stakeholder validation
when required; otherwise mark material direction assumed and vetoable.

For loop-zero tasks, record artifact path and material assumptions in
`.loopzero/task.md` Notes, then follow [the contract](../../CONTRACT.md) for
delivery checks after the owner commits. Finish when realistic states make the
decision inspectable, relevant checks work, controls are honest, and translation
gaps and assumptions are explicit.
