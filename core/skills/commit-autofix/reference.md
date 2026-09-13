# Commit Autofix — {{package.product_name}} Reference

## Hook Chain

{{skill_tokens.commit_hook_chain}}

## Frontend Dependency Preflight

{{skill_tokens.commit_dependency_preflight}}

## Commit Identity

`commit-auto-fix.sh` exports the {{package.commit_identity}} author/committer identity; direct commits are guarded by `{{toolchain.scripts_dir}}/hooks/commit-author-check.sh`.

## Safe Generated Outputs

The caller owns the commit scope. Commit-autofix may stage hook output only when
it belongs to a caller-staged file or one of these generated outputs:

| Trigger staged by caller | Safe generated outputs |
| ------------------------ | ---------------------- |
| `*router.py`, `*schemas.py`, `*main.py` (hook scope only — a response field added in another `app/**` module needs a manual `{{toolchain.commands.openapi_export}} && {{toolchain.commands.openapi_generate}}`) | `{{package.openapi_document}}`, `{{toolchain.frontend_dir}}/app/openapi-client/**` |
| `{{toolchain.uv}}.lock` or `{{toolchain.backend_dir}}/{{toolchain.uv}}.lock` | `{{toolchain.backend_dir}}/requirements.txt` |
{{skill_tokens.commit_mirror_paths}}
| `{{package.docs_dir}}/**/*.md` | managed docs index files such as `{{package.docs_dir}}/index.md` and `{{package.docs_dir}}/**/index.md` |
| Explicitly staged snapshot or `{{package.env_prefix}}_REFRESH_METERS=1` | `{{package.docs_dir}}/ops/meters-snapshot.json` |

Anything outside this table stops the workflow until the caller confirms it belongs in the commit.

## Scope-Gated Checks

{{skill_tokens.commit_backend_hook_behavior}}

**Docs changed** (`{{package.docs_dir}}/**`):
```bash
{{toolchain.commands.docs_verify}}
```

## Push / Handoff Gate

Before `git push` or the outermost agent handoff, run `{{toolchain.commands.ci_mirror_check}}`. Local mirror for checks that commonly fail post-push when staged-file pre-commit is green: conditional online blueprint drift, path-aware backend lint/mypy, path-aware frontend type-check/tests, and agent-config sync. Frontend validation stays related-test by default, but deterministically escalates to the full suite for shared mocks, route shells, server actions, generated clients, shared action/API adapters, at least 20 changed TS/TSX files, or `{{package.frontend_full_env}}=1`. Backend changed-tests were removed from the local mirror; use the focused owning test first and add targeted integration for DB/API/task paths. Seeded Playwright remains a user-flow handoff check, not a universal commit check. Nested skills skip this gate; the orchestrator runs it once.
