---
name: design-mockup
description: Explore a UI direction with a standalone HTML/CSS mockup before production implementation.
---

# Design Mockup

Make the UI decision inspectable in a clearly non-production artifact; omit
backend implementation. Read the request, design, existing surface and relevant
component, token and browser guidance. Identify actors, scenarios, consequential
states and the question to settle. Distinguish assumptions from settled decisions.

Default to portable standalone HTML/CSS with bounded inline interaction, semantic
responsive markup, realistic data and no remote runtime or production dependency.
Use the documented location or `docs/design/mockups/YYYY-MM-DD-feature-name.html`.
Controls must work or be plainly non-functional. Demonstrate transitions only
when they inform the decision; state production gaps and component mappings.

Inspect states, viewports and accessibility material to the decision, including
keyboard/focus, names, contrast, overflow and supported themes as relevant.
Report inspected conditions and unavailable checks honestly. Seek stakeholder
validation when required; otherwise label material direction assumed and vetoable.
Record the artifact and assumptions in task Notes and follow
[the contract](../../CONTRACT.md) for delivery. Finish by stating what the mockup
settled and the next decision.

After this step, resume the commissioned outcome within the authorized finish,
or hand over its remaining acceptance; after two unsuccessful repairs,
reconsider the route.
