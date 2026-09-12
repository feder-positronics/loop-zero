# Fix Failing Tests — {{package.product_name}} Commands

Use the narrowest lane that reproduces the observed failure. Preserve the test
runner's exit status and full failure location; do not pipe validation through
`tail`, `head`, or an unguarded filter.

## Environment

Use the shared `{{toolchain.backend_dir}}/.venv` and `{{toolchain.frontend_dir}}/node_modules`.
Start integration infrastructure with `{{toolchain.commands.db_test_start}}` only when needed.
If backend dependencies are genuinely stale, use the repository `{{toolchain.uv}}` setup;
if frontend dependencies are absent in a worktree, run `{{toolchain.commands.worktree_setup}}`
to restore the shared link before considering an install. Run the documented
frozen-lockfile install only when `package.json`, `{{toolchain.pnpm}}-lock.yaml`, or the
primary dependency cache is genuinely stale. Serialize either mutation under
the [worktree rule]({{package.rules_root}}/parallel-agents.mdc).

## Focused Reproduction

```bash
cd {{toolchain.backend_dir}} && {{toolchain.uv}} run {{toolchain.pytest}} tests/path/test_file.py::test_case -x -q
cd {{toolchain.backend_dir}} && {{toolchain.uv}} run {{toolchain.pytest}} tests/integration/<area>/test_X.py -x
{{toolchain.pnpm}} -C {{toolchain.frontend_dir}} exec {{toolchain.vitest}} run <file>
```

After an edit, rerun the focused owning test first. For a new backend module
or an ambiguous mapping, use the applicable precommit-unit lane from
`AGENTS.md`. Add targeted integration only when the touched DB/API path
requires it.

## Expanded Evidence

```bash
{{toolchain.commands.test_backend_unit}}
{{toolchain.pnpm}} -C {{toolchain.frontend_dir}} test
{{toolchain.pnpm}} -C {{toolchain.frontend_dir}} lint
{{toolchain.commands.test_backend}}
```

Run `{{toolchain.commands.test_backend}}` only for an explicitly requested full-backend cleanup, a
cross-cutting change, or a wave boundary after focused evidence passes. Frontend
type/build checks follow the current frontend guide and package scripts; do not
invent a stale command in this reference.
