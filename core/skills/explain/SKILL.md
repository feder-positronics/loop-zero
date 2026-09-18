---
name: explain
description: Explain a complex topic so a non-expert can orient, reason, and act; use Markdown by default, standalone HTML when requested, or an evidence-backed project briefing.
---

# Explain

Explain one complex topic while preserving the complexity that changes the
reader's understanding or action. Reduce cognitive load without overstating the
evidence or turning the explanation into implementation or a design verdict.

## Shape

Clarify audience, decision, depth, or output only when the ambiguity would
materially change the result. Orient first in plain language, then expose the
mechanism and only the nuance that matters. Define technical vocabulary at
first use.

Separate known facts, interpretation, uncertainty, and speculation. Source and
label material uncertainty for scientific, medical, regulatory, forecasting,
or emerging topics. Never imply more precision than the evidence supports or
hide a caveat that could reverse the reader's decision.

Use a visual only when it materially clarifies sequence, comparison, hierarchy,
ownership, branching, or spatial layout. Read [the progressive reference](reference.md)
for depth, visual choice, HTML validation, or briefing variants as needed.

## Output

- **Markdown:** answer inline using the smallest useful structure.
- **HTML:** render a self-contained, offline-safe, accessible, printable file to
  a temporary path and run the reference's mandatory checks. Promote it to the
  requested path or established explainer location only after they pass; a
  mandatory failure blocks promotion.
- **Briefing:** gather current repository and GitHub evidence for the requested
  project, backlog, pulse, strategy, or journey view. Treat GitHub as read-only
  and label source limits instead of repairing or inventing state.

End with a compressed, actionable or memorable takeaway. Use `review` for
critique, `backlog` for a live work view, `design-mockup` for UI exploration,
and `write-design-doc` when the explanation becomes a durable design contract.

## Exit

Markdown is done when the reader can state the main point, mechanism, material
uncertainty, and takeaway at the requested depth. HTML or briefing output also
requires its final path and size, source limits, validation result, and no
failed mandatory check or unauthorized state change.
