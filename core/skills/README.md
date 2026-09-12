# Skill Routing and Rendering

Top-level directories containing `SKILL.md` are loop-zero governance skills.
The `vendor/` subtree contains pinned methodology skills. Consumer product
skills remain consumer-owned. `loopzero sync` renders this file as the
canonical `<skills_dir>/README.md`, renders skills into `[package].skills_dir`,
and records exact managed paths, types, digests, directories, and mirrors in
`.loopzero/skills-manifest.json`.

## Template configuration

Package and toolchain tokens use the `package.<name>` and
`toolchain.<name>` names visible in the source templates. `product_name` and
`commit_identity` have no package defaults: a render that references either
requires the matching nonempty `[package]` key. Paths otherwise retain the
portable defaults implemented by the renderer. Command tokens are the known
keys under `[toolchain.commands]`; unknown keys are rejected.

Consumer-specific governance prose uses the known `[skill_tokens]` keys.
Product-skill dependencies use `[skill_routes]`, mapping a route key such as
`execute_blueprint` to an existing consumer skill directory. An omitted route
renders as “route unavailable in this consumer” and never links to a missing
skill. Substitution is literal and single-pass. Unknown, malformed, nested, or
unresolved tokens are errors; values may not contain managed markers.

### IntelFlo PR A compatibility block

IntelFlo PR A must add this exact block alongside its existing package and
toolchain values. These values, not loop-zero package defaults, preserve the
23 imported governance skills byte-for-byte:

````toml
[skill_tokens]
base_branch = "origin/main"
coherence_owner = "Marcin"
doctrine_d2 = "D-2"
doctrine_d14 = "D-14"
doctrine_d16 = "D-16"
doctrine_d18 = "D-18"
doctrine_d21 = "D-21"
legacy_delivery_contract = """
For a new task, use [loop-zero delivery](../../../docs/guides/dev-workflow/loop-zero-delivery.md)
under `AGENTS.md`'s Primary Delivery Contract. Retain issue acceptance and, when
applicable, blueprint lifecycle commands. The legacy procedure below applies
only to runs whose original record selects `intelflo-v1`; never migrate an
existing run, reset its history, or apply its phase choreography to a new task."""
debug_environment = """
- Backend API, SQLAlchemy, and error handling:
  [backend patterns](../../../docs/guides/backend/backend-patterns.md).
- Backend tests and the port-5433 recovery:
  [backend testing](../../../docs/guides/backend/backend-testing.md).
- UI state/network triage and isolated-stack evidence:
  [UI debugging](../../../docs/guides/frontend/bug-hunting-debug.md).
- Collaborative T3 inspection and bounded recovery:
  [T3 preview readiness and browser authority](../../../docs/guides/frontend/frontend-testing-e2e.md#preview-readiness-and-bounded-recovery).
- Frontend tests:
  [frontend testing](../../../docs/guides/frontend/frontend-testing.md).
- Production frontend and Vercel inspection:
  [production frontend](../../../docs/ops/runbooks/production-frontend-debugging.md)
  and [Vercel CLI](../../../docs/ops/runbooks/vercel-cli-guide.md)."""
commit_hook_chain = """
This repo has **two** pre-commit pipelines that `git commit` runs:

1. **pre-commit** (Python hooks via fastapi_backend venv):
   ```bash
   cd fastapi_backend && uv run pre-commit run
   ```
   Hooks: trailing-whitespace, ruff (lint + format), prettier, openapi-generate,
   sync-agent-rules, export-requirements, pytest-config-check,
   service-organization-check, alembic-heads-check, docs index generation,
   check-skills-canonical-dir.

2. **lint-staged** (frontend hooks via simple-git-hooks in `nextjs-frontend/package.json`):
   ```bash
   pnpm -C nextjs-frontend lint-staged
   ```
   Runs only when TS/TSX files are staged; `pre-commit run` does NOT trigger it.
   The commit-autofix script runs it after pre-commit so it validates the final
   staged TS/TSX state, including generated SDK files."""
commit_dependency_preflight = """If `nextjs-frontend/package.json` or `pnpm-lock.yaml` changed after a branch switch, merge, or cherry-pick, refresh deps before frontend validation: `pnpm -C nextjs-frontend install --frozen-lockfile`. In worktrees, a missing `lint-staged` is self-healed when staged TS/TSX or docs files require the shared dependency tree (`commit-auto-fix.sh` runs `make worktree-setup`; the runtime-entry hook pre-runs it). Backend-only commits do not need frontend dependency setup. Manual fix only if self-heal fails: `make worktree-setup` (worktree) / `pnpm -C nextjs-frontend install` (primary). Frontend `test` scripts fail fast on a stale Vitest binary; verify with `pnpm -C nextjs-frontend exec vitest --version` when needed."""
commit_mirror_paths = """| `.cursor/rules/*.mdc`, `.cursor/skills/*/SKILL.md` | `.agents/rules/*.md`, `.agent/rules/*`, `.claude/rules/*` |"""
commit_backend_hook_behavior = """
**Backend source changed** (`fastapi_backend/`): backend test-lane selection
(tooling vs full unit vs risk critical) follows the ladder in the AGENTS.md
Quick Commands table; `commit-auto-fix.sh` is the deterministic authority. Do
**not** substitute `make test-be` or `make test-be-slow` — those are broader
regression lanes, not commit-hook checks.

The full-unit xdist lane (`make test-be-precommit-unit`) is a convenience check, not the git pre-commit gate (staged-file hooks). It flakes under load — workers crash, or **diff-unrelated** tests fail (different set each run, pass in isolation). A failure outside the staged diff that passes alone is xdist pollution: re-run once (lower `PYTEST_WORKERS` if workers crashed), don't debug it (`reference_precommit_xdist_flakiness`).

**Agent-tooling tests only** (`fastapi_backend/tests/unit/scripts/`, with any
matching root `scripts/` implementation): `make test-agent-tooling`.
This lane runs the changed owning workflow-script tests (or the complete
workflow-script surface when no owning test is in scope) and skips the product
unit suite. Any product test, backend app, dependency, config, or shared test
infrastructure path keeps the full-unit fail-safe routing."""

[skill_routes]
audit_surface = "audit-surface"
design_handoff = "design-handoff"
design_mockup = "design-mockup"
execute_blueprint = "execute-blueprint"
fix_ui_bug = "fix-ui-bug"
fortify_roadmap = "fortify-roadmap"
frontier_roadmap = "frontier-roadmap"
generate_parser_rules = "generate-parser-rules"
implement_backend = "implement-backend"
implement_frontend = "implement-frontend"
````

## Maintenance Pipelines

The maintenance lane has two audit feeder→consumer pipelines and the
skill-reflection consumer. Author new janitor skills with this shape in mind.

```text
audit-health  ──┬─►  remove-dead-code   (consumes .audit/<today>/*_queue.json)
                ├─►  archive-docs       (consumes candidate manifest)
                └─►  audit findings     (test suite, coverage, guide drift)

code-quality-drift  ──┬─►  decompose-module  (consumes density findings)
                      ├─►  fix-* skills      (per finding, via resolve-findings)
                      ├─►  resolve-findings  (judgmental routing)
                      └─►  contract prune    (Contract lens → contract-prune-runbook queue → work-issue, one id/PR)

skill-reflection rule (in .agents/rules/)  ──►  skill-health
```

The four `disable-model-invocation: true` skills (`audit-health`,
`code-quality-drift`, `archive-docs`, `remove-dead-code`) never fire as a side
effect of ordinary edits. They consume queues only on explicit invocation and
a deliberate cadence. The user and the autonomous `backlog-drain` improvement
loop hold that invocation authority. The loop acts as the user; the invocation
flags stay. This section is the canonical home of that authority grant.

## Vendored precedence

For a rendered vendored skill, upstream `SKILL.md` is the byte-identical base.
The local `OVERLAY.md` is appended in a delimited managed section. On conflict,
the overlay wins over upstream and the [core contract]({{package.core_contract_from_readme}})
wins over both.
