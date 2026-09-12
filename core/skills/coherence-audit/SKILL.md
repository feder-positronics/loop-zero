---
name: coherence-audit
description: >-
  Audit a module or the whole product for composition, UI consistency,
  value-to-weight, completeness, and API-contract coherence. Produce a ranked
  wire-list and evidence-backed cut-list. Use dossier mode to assess one named
  capability in plain language for a non-technical reader. For a committable
  hardening roadmap use `{{skill_routes.fortify_roadmap.name}}`.
---

# Coherence Audit

Answer whether {{package.product_name}} behaves like one intentional product rather than a set
of individually plausible features.

When standalone, use a dedicated worktree for durable reports/artifacts; nested
runs inherit the orchestrator worktree under the [parallel-agent rule]. Use the
[coherence reference] for evidence-census techniques and the report skeleton.

## Grounding

Run `{{toolchain.commands.orient}}`; read [Vision], [atlas], [users/jobs], [owner doctrine],
[graveyard], [open questions], [owner inbox], [claims], [scenarios], and the
[component catalog]. Run `{{toolchain.commands.product_pulse}}` for usage evidence.
Respect {{skill_tokens.doctrine_d14}} in [owner doctrine] before producing owner decisions. Inspect
actual UI/API/runtime paths when a claim depends on them.

## Lenses

- **Composition** — built-but-unwired capabilities, duplicated jobs, missing
  cross-feature interactions, divergent state definitions.
- **UI consistency** — nav homes, catalog reuse, interaction/status vocabulary,
  truthful empty/loading/error states.
- **Value vs weight** — named scenario and usage evidence versus maintenance,
  operational, and cognitive cost.
- **Completeness** — stubs, inert controls, missing error/permission paths,
  half-delivered workflows.
- **API contract** — consumer parity, public-contract consistency, duplicate
  endpoints/read models, and user-isolation semantics.

The agent chooses the sweep strategy and may narrow around the strongest
evidence. Do not turn these lenses into an equal-weight checklist.

## Dossier Branch

When the request assesses one named capability for a non-technical
decision-maker, produce a **dossier** rather than the proposal report below:
the same evidence, carrying the [explanation contract]'s register for a reader
who decides without reading code. Open with what the capability is, why it
exists, and how it works today. Add a scale lens — whether the current shape
holds at the next order of magnitude of users, sources, and volume — and locate
it under [North Star]. Keep the earns-its-place verdict. The [dossier skeleton]
replaces the numbered output list in this mode; the persistence, authority, and
no-implementation rules still apply.

## Output

Produce:

1. a short system assessment;
2. ranked proposals with concrete evidence;
3. a **wire-list** of capabilities that should compose and how;
4. a separate **cut-list** with usage/scenario evidence and uncertainty;
5. open questions with deciding triggers and evidence.

Wire-list items can become issues autonomously inside {{skill_tokens.doctrine_d18}} authority. Cut-list
items are proposals only: {{skill_tokens.doctrine_d2}} reserves final user-facing cuts for {{skill_tokens.coherence_owner}}. High-
weight structural calls use propose-and-challenge and route through
[resolve-findings].

Persist the report and all questions/decisions; nothing strategic remains only
in chat. Follow the [producer boundary] and route campaign-relevant lenses to
the owning tracker for evaluation inside its `{{skill_routes.audit_surface.name}}` pass. Do not
implement proposals inside the audit.

[parallel-agent rule]: {{package.rules_root}}/parallel-agents.mdc
[coherence reference]: reference.md
[dossier skeleton]: reference.md#dossier-skeleton
[explanation contract]: ../explain/SKILL.md#explanation-contract
[North Star]: {{package.rules_root}}/design.mdc#north-star
[Vision]: {{package.docs_root}}/design/vision.md
[atlas]: {{package.docs_root}}/design/atlas.md
[users/jobs]: {{package.docs_root}}/design/users-and-jobs.md
[owner doctrine]: {{package.docs_root}}/design/owner-doctrine.md
[graveyard]: {{package.docs_root}}/design/graveyard.md
[open questions]: {{package.docs_root}}/design/open-questions.md
[owner inbox]: {{package.docs_root}}/design/owner-inbox.md
[claims]: {{package.docs_root}}/design/claims-manifest.yaml
[scenarios]: {{package.docs_root}}/scenarios/index.md
[component catalog]: {{package.docs_root}}/components/catalog.md
[resolve-findings]: ../resolve-findings/SKILL.md
[producer boundary]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md#finding-producer-boundary

## Exit Criteria

- [ ] Capabilities in scope are classified as wired/unwired/dead/duplicate.
- [ ] Producer→consumer, missed-composition, pattern/nav/grammar, placeholder,
  orphan, and half-wired evidence was considered where relevant.
- [ ] Every recommendation traces to a user job, evidence, and inspected path.
- [ ] Wire/cut lists are separately ranked, persisted, and routed under {{skill_tokens.doctrine_d2}}.
- [ ] No speculative frontier generation or implementation was mixed in.
- [ ] When dossier mode applies, it states mechanism, evidence tier, scale, and
  verdict in language its non-technical reader can act on.
