# Commit Autofix — {{package.product_name}} Reference

## Hook Chain

This repo has **two** pre-commit pipelines that `git commit` runs:

1. **pre-commit** (Python hooks via {{toolchain.backend_dir}} venv):
   ```bash
   cd {{toolchain.backend_dir}} && {{toolchain.uv}} run pre-commit run
   ```
   Hooks: trailing-whitespace, ruff (lint + format), prettier, openapi-generate,
   sync-agent-rules, export-requirements, {{toolchain.pytest}}-config-check,
   service-organization-check, alembic-heads-check, docs index generation,
   check-skills-canonical-dir.

2. **lint-staged** (frontend hooks via simple-git-hooks in `{{toolchain.frontend_dir}}/package.json`):
   ```bash
   {{toolchain.pnpm}} -C {{toolchain.frontend_dir}} lint-staged
   ```
   Runs only when TS/TSX files are staged; `pre-commit run` does NOT trigger it.
   The commit-autofix script runs it after pre-commit so it validates the final
   staged TS/TSX state, including generated SDK files.

## Frontend Dependency Preflight

If `{{toolchain.frontend_dir}}/package.json` or `{{toolchain.pnpm}}-lock.yaml` changed after a branch switch, merge, or cherry-pick, refresh deps before frontend validation: `{{toolchain.pnpm}} -C {{toolchain.frontend_dir}} install --frozen-lockfile`. In worktrees, a missing `lint-staged` is self-healed when staged TS/TSX or docs files require the shared dependency tree (`commit-auto-fix.sh` runs `{{toolchain.commands.worktree_setup}}`; the runtime-entry hook pre-runs it). Backend-only commits do not need frontend dependency setup. Manual fix only if self-heal fails: `{{toolchain.commands.worktree_setup}}` (worktree) / `{{toolchain.pnpm}} -C {{toolchain.frontend_dir}} install` (primary). Frontend `test` scripts fail fast on a stale Vitest binary; verify with `{{toolchain.pnpm}} -C {{toolchain.frontend_dir}} exec {{toolchain.vitest}} --version` when needed.

## Commit Identity

`commit-auto-fix.sh` exports the {{package.commit_identity}} author/committer identity; direct commits are guarded by `{{toolchain.scripts_dir}}/hooks/commit-author-check.sh`.

## Safe Generated Outputs

The caller owns the commit scope. Commit-autofix may stage hook output only when
it belongs to a caller-staged file or one of these generated outputs:

| Trigger staged by caller | Safe generated outputs |
| ------------------------ | ---------------------- |
| `*router.py`, `*schemas.py`, `*main.py` (hook scope only — a response field added in another `app/**` module needs a manual `{{toolchain.commands.openapi_export}} && {{toolchain.commands.openapi_generate}}`) | `{{package.openapi_document}}`, `{{toolchain.frontend_dir}}/app/openapi-client/**` |
| `{{toolchain.uv}}.lock` or `{{toolchain.backend_dir}}/{{toolchain.uv}}.lock` | `{{toolchain.backend_dir}}/requirements.txt` |
| `.cursor/rules/*.mdc`, `.cursor/skills/*/SKILL.md` | `.agents/rules/*.md`, `.agent/rules/*`, `.claude/rules/*` |
| `{{package.docs_dir}}/**/*.md` | managed docs index files such as `{{package.docs_dir}}/index.md` and `{{package.docs_dir}}/**/index.md` |
| Explicitly staged snapshot or `{{package.env_prefix}}_REFRESH_METERS=1` | `{{package.docs_dir}}/ops/meters-snapshot.json` |

Anything outside this table stops the workflow until the caller confirms it belongs in the commit.

## Scope-Gated Checks

**Backend source changed** (`{{toolchain.backend_dir}}/`): backend test-lane selection
(tooling vs full unit vs risk critical) follows the ladder in the AGENTS.md
Quick Commands table; `commit-auto-fix.sh` is the deterministic authority. Do
**not** substitute `{{toolchain.commands.test_backend}}` or `{{toolchain.commands.test_backend_slow}}` — those are broader
regression lanes, not commit-hook checks.

The full-unit xdist lane (`{{toolchain.commands.test_backend_precommit}}`) is a convenience check, not the git pre-commit gate (staged-file hooks). It flakes under load — workers crash, or **diff-unrelated** tests fail (different set each run, pass in isolation). A failure outside the staged diff that passes alone is xdist pollution: re-run once (lower `PYTEST_WORKERS` if workers crashed), don't debug it (`reference_precommit_xdist_flakiness`).

**Agent-tooling tests only** (`{{toolchain.backend_dir}}/tests/unit/{{toolchain.scripts_dir}}/`, with any
matching root `{{toolchain.scripts_dir}}/` implementation): `{{toolchain.commands.test_agent_tooling}}`.
This lane runs the changed owning workflow-script tests (or the complete
workflow-script surface when no owning test is in scope) and skips the product
unit suite. Any product test, backend app, dependency, config, or shared test
infrastructure path keeps the full-unit fail-safe routing.

**Docs changed** (`{{package.docs_dir}}/**`):
```bash
{{toolchain.commands.docs_verify}}
```

## Push / Handoff Gate

Before `git push` or the outermost agent handoff, run `{{toolchain.commands.ci_mirror_check}}`. Local mirror for checks that commonly fail post-push when staged-file pre-commit is green: conditional online blueprint drift, path-aware backend lint/mypy, path-aware frontend type-check/tests, and agent-config sync. Frontend validation stays related-test by default, but deterministically escalates to the full suite for shared mocks, route shells, server actions, generated clients, shared action/API adapters, at least 20 changed TS/TSX files, or `{{package.frontend_full_env}}=1`. Backend changed-tests were removed from the local mirror; use the focused owning test first and add targeted integration for DB/API/task paths. Seeded Playwright remains a user-flow handoff check, not a universal commit check. Nested skills skip this gate; the orchestrator runs it once.
