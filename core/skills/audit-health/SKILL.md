---
name: audit-health
description: >-
  Run deterministic repository, test-suite, documentation, or gate health
  collectors and turn valid evidence into a small routed queue. Use for periodic
  hygiene audits that route proposals to their owning skills; for delivering a
  routed item use `work-issue`.
disable-model-invocation: true
---

# Health Audit

Interpret collector evidence without reimplementing collectors or converting
weak signals into unsupported work.

## Contracts

- Invocation authority (user or the autonomous improvement loop, on its own
  cadence) is defined once in the
  [skill README's Maintenance Pipelines note]({{package.skills_readme}}#maintenance-pipelines).
- Use the [{{package.product_name}} collector reference](./reference.md) for commands, artifacts,
  status semantics, and mode inputs; load the [deep audit rubric]({{package.docs_root}}/guides/reference/audit-health-reference.md)
  only when scoring or extended triage is needed.
- Apply repository [`suppressions.yaml`]({{package.suppressions_file}}), open
  learnings, current git scope, and the collector's own freshness/status record.
- Route code-standard drift to [`code-quality-drift`](../code-quality-drift/SKILL.md),
  evidenced file/module removal to [`remove-dead-code`](../remove-dead-code/SKILL.md),
  stale documentation to [`archive-docs`](../archive-docs/SKILL.md), test gaps to
  [`write-tests`](../write-tests/SKILL.md), and skill-system drift to
  [`skill-health`](../skill-health/SKILL.md).

## Evidence and Judgment

- Select the named mode: broad/backend/frontend health, Python dead
  code/coverage, test-suite quality, guides, or gated blueprints. Consume every
  required mode artifact before reaching a verdict.
- Treat missing, stale, invalid, or `infra_unavailable` collectors as reduced or
  absent evidence, never as healthy. Separate infrastructure failure from
  repository failure. Route whole-file dead-code evidence to
  [`remove-dead-code`](../remove-dead-code/SKILL.md) and live symbol cleanup to
  [`refine-code`](../refine-code/SKILL.md).
- Dossier liveness (guides mode): every strategic dossier under
  `{{package.docs_dir}}/design/dossiers/` must keep a live claim set — each gap names
  `target-claims:` ids registered in the claims-manifest `dossier:` group or
  carries an explicit `unverifiable: <reason>` marker, and every named id
  still exists in the manifest. A dossier failing this is a proposal routed to
  the design lane (design-engine blueprint: dossiers must not decay into
  unverified prose).
- Coverage, density, timing, dependency, auth-route, graph, and similar numeric
  signals are leads. Establish affected behavior, risk, confidence, and a
  narrow owner before queueing work; do not reward percentage movement alone.

## Authority and Output

- Collector-owned `{{package.audit_root}}/YYYY-MM-DD/` artifacts and documented cache cleanup
  are in scope. Do not edit application/docs content, open reminder issues,
  mutate blueprint lifecycle, or implement a proposal during the audit.
- Write the dated `/tmp` report defined by the reference: collector status,
  freshness, infrastructure, limits, blockers first, and a PR-sized queue with
  severity, evidence, confidence, affected behavior, and owner skill.
- Follow the [producer boundary]. Campaign-relevant health lenses route to the
  owning tracker for the single `{{skill_routes.audit_surface.name}}` pass.

## Done When

- Every verdict cites valid mode evidence and explicit limits; required signals
  were consumed; suppressions and learnings were reconciled; and important
  proposals are narrowly routed without creating a shadow backlog or changing
  repository state beyond collector-owned artifacts.

[producer boundary]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md#finding-producer-boundary
