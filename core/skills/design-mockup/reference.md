# Design Mockup Reference

## Preflight

Inspect the repository's component catalog, theme and layout guidance, prior
mockups, and browser-testing instructions when they exist. If they do not, infer
only what current production UI demonstrates and state the limitation.

## Artifact Convention

Use `docs/design/mockups/YYYY-MM-DD-feature-name.html`: ISO date, short kebab-case
feature name, one self-contained HTML file. Keep CSS and bounded interaction
JavaScript inline, avoid remote runtime dependencies, and title the page as a
mockup. Do not add production imports or require an application server merely
to inspect it.

## Representative Evidence

Choose data that exposes the design's hard cases: long labels, empty and loading
states, errors, permission differences, density, truncation, or destructive
actions when relevant. Avoid lorem ipsum when domain-realistic content affects
layout or judgment.

Show enough interaction to evaluate state transitions, hierarchy, and feedback.
Static alternatives are preferable to fake controls when behavior is not part
of the design question.

## Inspection

Inspect at least one representative desktop and mobile width. Check keyboard
order and visible focus, headings and landmarks, control names, contrast,
reduced motion, overflow, zoom, and light/dark themes when the product supports
both. Report what was inspected and any browser-dependent check that could not
run.
