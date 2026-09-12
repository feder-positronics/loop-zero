# Health Audit — {{package.product_name}} Collector Reference

Load for exact collector commands, artifacts, and signal-status semantics. Use
the [deep audit rubric]({{package.docs_root}}/guides/reference/audit-health-reference.md)
only for extended docs lenses, prioritization, coverage triage, or report shape.

## Commands by Mode

| Mode | Canonical command |
| --- | --- |
| broad | `{{toolchain.commands.health}}` |
| backend | `{{toolchain.commands.health_backend}}` |
| frontend | `{{toolchain.commands.health_frontend}}` |
| test-suite quality | `{{toolchain.commands.health}}`, then interpret its test/coverage artifacts with the linked testing contracts |
| Python dead code | `{{toolchain.commands.audit_python_dead_code}}` |
| Python coverage | `{{toolchain.commands.audit_python_coverage}}` |
| guides | `{{toolchain.commands.docs_verify}}` then `{{toolchain.commands.docs_audit}}` |
| gated blueprints | `{{toolchain.commands.gates_verify}}` |

For every mode, include open learnings with `{{toolchain.commands.learnings_status}}`; learnings
are cross-cutting audit context, not a standalone mode.

`{{toolchain.commands.health}}` and its scripts own cache cleanup, artifact creation, surface
hygiene, docs collection, status aggregation, and failure propagation. Do not
recreate their shell pipelines inside the skill.

## Artifact and Status Contract

The selected collector writes under `{{package.audit_root}}/YYYY-MM-DD/`. Read `status.json`
or the mode's generated status summary before interpreting payload artifacts.

| Evidence | Typical artifact |
| --- | --- |
| queue + signal integrity | `python_queue.json` / `python_queue.md` |
| backend health | `mypy.txt`, `ruff.json`, `coverage.txt`, `coverage.json`, `vulture.txt` |
| frontend health | `tsc.txt`, `eslint.json` (`eslint.txt` is stderr), `tsprune.txt`, `madge.txt`, `build.txt` |
| docs and lifecycle | `docs_verify.txt`, `docs_manifest.txt`, `docs_audit_target.txt` |
| dependency/vulnerability | `outdated-*`, `audit-*` |
| public env/auth/maintenance/queue | `next-public-env.txt`, `route-auth-signal.txt`, `disabled-maintenance.txt`, `queue-health.txt` |
| graph | `graph-*.json`, `graph-summary-*.md` when requested |
| learnings | `learnings_status.txt`, `learnings_verify.txt` |

- A missing, stale, invalid, or failed payload inherits the collector status; it
  is not a clean zero.
- Coverage with `status: "infra_unavailable"` is unmeasured/deferred. For a real
  reading, run `{{toolchain.commands.infra_start}}` and rerun the collector.
- `symbol_cleanup` entries are unused symbols inside live files; route them to
  [`refine-code`](../refine-code/SKILL.md), not the quarantine workflow.
- Network-backed dependency/vulnerability artifacts may be inconclusive when
  registry access fails; preserve their status and stderr instead of treating an
  empty file as success.

## Test and Guide Inputs

Guide-mode dossier-liveness inputs are `{{package.docs_dir}}/design/dossiers/*.md` (gap tables
and `target-claims:`/`unverifiable:` markers) plus the `dossier:` group in
`{{package.docs_dir}}/design/claims-manifest.yaml`; `{{toolchain.commands.claims_check}}` output is the claim
state signal. The check itself is defined in SKILL.md.

For test-mode interpretation, use the canonical [backend]({{package.docs_root}}/guides/backend/backend-testing.md)
and [frontend]({{package.docs_root}}/guides/frontend/frontend-testing.md) contracts for
lane, isolation, marker, and coverage meaning. For guide mode, apply the
[docs lifecycle policy]({{package.docs_root}}/policies/docs-lifecycle.yaml) after the
deterministic checks; human review is limited to flagged files.
