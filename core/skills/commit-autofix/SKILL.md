---
name: commit-autofix
description: >-
  Prepare an agent-owned commit or fix commit-hook failures by running the real
  pre-commit pipeline, staging only safe hook outputs, and getting the commit
  boundary back to green. Quick fix for backend and frontend. Use before any
  agent-run git commit or when git commit fails due to hooks.
---

# Commit Autofix

Prepare the caller-owned commit scope so `git commit` passes hooks on the first
try. Run the repository script, not a simulated subset of its hooks:

```bash
./{{toolchain.scripts_dir}}/util/commit-auto-fix.sh
```

Pass a message only when the caller explicitly requested the commit:

```bash
./{{toolchain.scripts_dir}}/util/commit-auto-fix.sh "type(scope): message"
```

The executable authority is [`commit-auto-fix.sh`]({{toolchain.scripts_root}}/util/commit-auto-fix.sh).
Use [reference.md](reference.md) only for conditional commands, generated-output
rules, and recovery detail.

## Contract

- The caller stages the intended commit boundary; `commit-autofix` must not infer or widen it.
- The script runs pre-commit on staged files only (never `--all-files`) and
  fails closed on nothing-staged, same-file staged/unstaged overlap,
  and out-of-scope generator output; never bypass or reorder it.
- Never use broad `git add -u` or `git add .` inside this workflow.
- Unexpected output, concurrent state change, restoration conflict, or a broad
  failure stops safely with the caller's work preserved.

## Failure Triage

For a narrow script failure, apply the minimal in-scope fix, run the affected
check, restage only the caller-owned path, then rerun the full script. Route a
behavioral test failure to `fix-failing-tests`; return broad, unclear, or
scope-expanding failures to the caller rather than suppressing validation.

## Evidence And Done

Report the starting staged paths, script invocation, safe generated outputs,
applicable hook/test/docs results, and either the commit SHA or the preserved
uncommitted state. The workflow is complete only when the script exits cleanly,
the final boundary remains caller-owned, and no temporary recovery state is
left unresolved.
