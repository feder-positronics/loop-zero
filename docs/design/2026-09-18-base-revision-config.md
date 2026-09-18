# Read `[checks]` from the base revision

Date: 2026-09-18. Status: proposed.

## Failure today

`loopzero check` and `loopzero ready` load `workflow.toml` from the worktree
(`cli._load_config`). A candidate can therefore set `checks.commands = ["true"]`
or `checks.required_ci = []`, run `check`, and produce a clean report that
`ready` accepts. Branch protection and PR CI remain the enforced gates, so
this is a guardrail against accidental or agent-driven weakening, not a
tamper-proof boundary. The contract already says readiness is a checklist,
not a boundary; this change keeps that honesty.

## Change

- `config.load` gains a companion `load_base(worktree, base_branch)` that
  reads `workflow.toml` with `git show origin/<base>:workflow.toml` and
  returns the parsed `[checks]` table. The `[repo]` and `[delivery]` tables
  still come from the worktree copy.
- `check`, `ready`, `merge` and `status` build their `Config` with the base
  `[checks]` table overlaid on the worktree file.
- Fallback to the worktree `[checks]` happens only when `git show` reports
  the path does not exist on base. Malformed TOML on base or any other Git
  error fails the command with the underlying message.
- The `--config` flag keeps its meaning: an explicit path disables the base
  overlay and prints a one-line notice that checks are unpinned.
- `_primary_base` currently falls back to the task file's base SHA when
  `origin/<base>` is missing. The overlay uses the same resolution, so both
  paths agree on what "base" means.

## Trade-off

A PR that adds or changes a check does not exercise it locally until merged.
PR CI still runs it, because the workflow file is part of the candidate.
Print `checks pinned to origin/<base>@<sha>` at the top of `check` output so
the difference is visible when someone wonders why a new command did not run.

## Tests

- Worktree sets `commands = ["true"]`; base has the real command; `check`
  runs the real command.
- Base lacks `workflow.toml`; `check` uses the worktree file.
- Base file is malformed; `check` fails with the parse error.
- `ready` uses base `required_ci` when the worktree empties the list.

## Cost

30–60 lines in `config.py` and `cli.py`, four tests. No new state.
