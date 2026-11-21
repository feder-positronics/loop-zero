---
name: explain
description: Explain a complex topic in Markdown, requested standalone HTML, or an evidence-backed project briefing.
---

# Explain

Make one complex topic understandable without losing complexity that changes the
reader's action. Explain rather than implement or deliver a design verdict.

Clarify audience, decision, depth or format only when ambiguity materially affects
the result. By default give the intuition, the mechanism and the caveats that would
change a decision. A reader who only needs direction gets orientation alone; one
who must apply it unaided also gets edge cases and sources. Define technical
terms on first use.

Separate facts, interpretation, uncertainty and speculation. Source material
uncertainty in scientific, medical, regulatory, forecasting and emerging topics;
avoid unsupported precision or caveats hidden from decisions.

Answer the question the reader actually has, against what they expected. Orient
first, explain the mechanism and say how each step leads to the next, then give
an actionable or memorable takeaway. Cut details that are interesting but do not
help the reader follow, judge or act. Choose structure to fit, using
components, data flow and failure/authority boundaries for technical topics. Before sending, check that
each takeaway follows from the concrete material and says something the opening
did not.

When the topic has a shape (steps or branches, states, a comparison,
quantities over time, components and boundaries), show it once in the form that
fits: flowchart, state table, comparison table, chart or box diagram. In Markdown
prefer tables and lists that stay legible as plain text; use Mermaid only where
the destination is known to render it, such as T3 chat and GitHub. Every visual
must carry something the prose does not, and the prose must stand without it.

- **Markdown:** answer inline with the smallest useful structure.
- **HTML:** follow [HTML validation](reference.md#html-validation); report final
  path, size, source limits and validation.
- **Briefing:** follow [briefing variants](reference.md#briefing-variants).
  Use [backlog](../backlog/SKILL.md) for live work evidence; keep GitHub read-only.

Exit when the reader can state the main point, mechanism, material uncertainty
and takeaway at the requested depth, with source limits explicit and no
unauthorized state changes. Route critique to [review](../review/SKILL.md),
UI exploration to [design-mockup](../design-mockup/SKILL.md), and durable design
contracts to [write-design-doc](../write-design-doc/SKILL.md).
