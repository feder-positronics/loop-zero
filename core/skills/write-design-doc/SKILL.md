---
name: write-design-doc
description: "Create or refine a durable design contract: exploration, ADR, initiative, blueprint or guide."
---

# Write Design Doc

Produce the smallest artifact that lets the next actor proceed without
re-deriving intent or architecture. Do not implement it. Unless explicitly
requested, skip the document for a clear, small, safely bounded change.

Read only Grounding and Design Contract in [the reference](reference.md) up front.
Read Artifact Choice if undecided, Decisions for material choices, Artifact
Lifecycle for relationships or time gates, and Validate and the applicable
Type-Specific Checks before handoff. Resolve ambiguity and keep blockers owned.

Follow [Reporting](../../CONTRACT.md#reporting) for the owner-facing opening
and visuals. Write user scenarios before UI proposals, then validate material
UI shape with [design-mockup](../design-mockup/SKILL.md) or an established
pattern. Blueprints name concrete paths and sequenced acceptance without
prescribing inferable coding steps.

For an existing artifact, preserve settled decisions unless evidence invalidates
their premises. Correct material gaps and verify the underlying design problem
is resolved beyond wording. Suggestions never become blockers; unresolved
material questions retain owners and visibly block the relevant action.

Validate the artifact. Revise material ambiguity or leave an explicit blocker.
Finish with a commit-ready document and actionable acceptance and questions.
Record its path and material decisions in task Notes, or otherwise in the handoff.

Then continue the task you were given as far as it authorizes, or hand over
what is left of its acceptance. If two repairs have failed, the route is
probably wrong, so reconsider it before a third.
