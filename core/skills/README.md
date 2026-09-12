# Skills source and substitutions

Top-level directories containing `SKILL.md` are loop-zero governance skills.
The `vendor/` subtree contains pinned methodology skills. Consumer product
skills are never copied into this snapshot and remain owned by the consumer.
`loopzero sync` renders templates into `[package].skills_dir`; it refuses to
replace an unmanifested consumer skill and records managed files in
`.loopzero/skills-manifest.json`.

## Template tokens

Only the following tokens are valid. Values come from `[package]` and
`[toolchain]` in `workflow.toml`; omitted values use the compatibility defaults
shown here, which reproduce IntelFlo's existing layout.

| Token | Compatibility default |
|---|---|
| `{{package.product_name}}` | `IntelFlo` |
| `{{package.env_prefix}}` | `[package].env_prefix` (`LOOPZERO` when absent) |
| `{{package.primary_env}}` | `<env_prefix>_PRIMARY` |
| `{{package.audit_root}}` | `[package].audit_root` (`.audit` when absent) |
| `{{package.docs_root}}` | `../../../docs` |
| `{{package.rules_root}}` | `../../rules` |
| `{{package.constraints_file}}` | `../../../AGENTS.md` |
| `{{package.delivery_guide}}` | `../../../docs/guides/dev-workflow/loop-zero-delivery.md` |
| `{{package.skills_readme}}` | `../README.md` |
| `{{package.docs_dir}}` | `docs` |
| `{{package.weekly_issue_workflow}}` | `.github/workflows/weekly-issue.yml` |
| `{{package.openapi_document}}` | `shared-data/openapi.json` |
| `{{package.commit_identity}}` | `Vercel-authorized eowca` |
| `{{package.frontend_full_env}}` | `CI_MIRROR_FULL_FRONTEND` |
| `{{package.suppressions_file}}` | `../../../suppressions.yaml` |
| `{{package.core_contract}}` | consumer-relative path to `[core].path/CONTRACT.md` |
| `{{package.core_handoff}}` | consumer-relative path to `[core].path/HANDOFF.md` |
| `{{toolchain.backend_dir}}` | `fastapi_backend` |
| `{{toolchain.frontend_dir}}` | `nextjs-frontend` |
| `{{toolchain.scripts_dir}}` | `scripts` |
| `{{toolchain.scripts_root}}` | `../../../scripts` |
| `{{toolchain.python}}` / `{{toolchain.system_python}}` | `python3` / `/usr/bin/python3` |
| `{{toolchain.uv}}` / `{{toolchain.pnpm}}` | `uv` / `pnpm` |
| `{{toolchain.pytest}}` / `{{toolchain.vitest}}` | `pytest` / `vitest` |

Command tokens use `{{toolchain.commands.<name>}}`. The supported names are:
`audit_graph`, `audit_graph_backend`, `audit_graph_frontend`,
`audit_python_coverage`, `audit_python_dead_code`, `backlog`,
`backlog_delegation`, `check_skills`, `ci_mirror_check`, `claims_check`,
`db_test_start`, `delivery_status`, `docs_archive_candidates`, `docs_audit`,
`docs_verify`, `gates_verify`, `health`, `health_backend`, `health_frontend`,
`infra_start`, `learnings_status`, `metabolism_status`, `meters_snapshot`,
`openapi_export`, `openapi_generate`, `orient`, `phase_stats`, `product_pulse`,
`reentry`, `reentry_coverage`, `reflection_digest`, `skill_stats`,
`test_agent_tooling`, `test_backend`, `test_backend_precommit`,
`test_backend_slow`, `test_backend_unit`, `weekly_issue`, `worktree_claims`, and
`worktree_setup`. Their compatibility defaults are the same hyphenated
`make <target>` invocations found in IntelFlo; the health and metrics variants
retain their existing arguments.

Substitution is literal and single-pass. Unknown or unresolved tokens are an
error. Values may not contain control characters or loop-zero managed markers.

## Vendored precedence

For a rendered vendored skill, upstream `SKILL.md` is the byte-identical base.
The local `OVERLAY.md` is appended in a delimited managed section. On conflict,
the overlay wins over upstream and [`core/CONTRACT.md`](../CONTRACT.md) wins
over both.
