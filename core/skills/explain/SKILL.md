---
name: explain
description: >-
  Explain a complex topic so a non-expert can orient, reason, and act. Default
  to Markdown; use `output mode: html` for an offline printable explainer and
  `briefing <variant>` for evidence-backed project status. For critiquing a
  design artifact use `review-design-doc`.
---

# Explain

Preserve the topic's decision-relevant complexity while reducing the reader's
cognitive load and keeping claims traceable to evidence.

## Contracts

- Align only when audience, decision, depth, or output ambiguity would change
  the result, under the [alignment rule]({{package.rules_root}}/align-gate.mdc).
- For Markdown, consult the [depth and visual guidance](./reference.md#depth-and-shape)
  only when needed. HTML must apply the [render contract](./reference.md#html-render-contract);
  briefings must apply the [variant and live-evidence contract](./reference.md#briefing-variants).
  Use the deep [explanation-design]({{package.docs_root}}/guides/reference/explanation-design-reference.md)
  and [HTML artifact]({{package.docs_root}}/guides/reference/html-artifact-tech-reference.md)
  references only when their specialist detail is needed.
- HTML/briefing writes own a standalone worktree or inherit the outer mutating
  workflow's worktree under the [parallel-agents rule]({{package.rules_root}}/parallel-agents.mdc).
  Use the [default template](./templates/premium-minimal-wide.html) and the
  canonical [docs destinations]({{package.docs_root}}/guides/reference/docs.md).

## Explanation Contract

- Orient first in plain language, then expose mechanism and only the nuance that
  changes understanding or action. Bridge technical vocabulary at first use.
- Separate known facts, interpretation, uncertainty, and speculation. For
  scientific, medical, regulatory, forecasting, or emerging topics, source and
  label material uncertainty; never make confidence more precise than evidence.
- Use a visual only when it materially clarifies sequence, comparison,
  hierarchy, ownership, branching, or spatial layout. A reader who stops after
  the orientation must still retain the main point.
- End with a compressed, actionable or memorable takeaway. Do not hide a caveat
  that could reverse the reader's decision.

## Output Modes

- **Markdown** — answer inline using the smallest useful structure.
- **HTML** — render the same evidence-backed outline to a self-contained,
  offline-safe, accessible, printable file. Render to a temporary path, pass the
  mandatory structure/source-fidelity/security/offline/determinism/size checks
  in the reference, then promote it to `{{package.docs_dir}}/explainers/` or the user path.
  Report browser-dependent checks separately and never claim full validation
  when tooling was unavailable.
- **Briefing** — choose the named project/backlog/pulse/strategy/journey variant
  from the reference, gather current repository and GitHub evidence, and write
  to `{{package.docs_dir}}/reports/`. Briefing is read-only on GitHub and lifecycle/design state;
  report drift and source limits instead of repairing or inventing state.

## Authority and Done

- Do not turn an explanation into implementation, a design decision, a review
  verdict, or an external publication without separate authority. Stop or label
  the limit when required live/specialist evidence cannot be obtained.
- Markdown is done when the reader can state the main point, mechanism, material
  uncertainty, and takeaway at the requested depth. HTML/briefing output also
  requires the reference's validation report, final path/size, source limits,
  and no failed mandatory gate or unauthorized state mutation.

## Routes

- Use [`review`](../review/SKILL.md) for critique, [`backlog`](../backlog/SKILL.md)
  for a live backlog view, [`design-mockup`](../design-mockup/SKILL.md) for UI
  exploration, and [`write-design-doc`](../write-design-doc/SKILL.md) when the
  explanation becomes a durable design contract.
