# Re-sample HEAD and dirtiness after long runs

Date: 2026-09-18. Status: proposed.

## Failure today

`sandbox.run_checks` reads HEAD and `git status` once, before the commands
run. `cli.cmd_review` requires a clean tree before the model starts. Both
runs take minutes. An agent that commits or edits during that window ends
with a check report or a review whose `head` field names a commit the
commands did not fully see. The worktree is bound read-only inside bwrap,
but host edits are visible through the bind. `ready` then accepts the report
because its `head` matches.

## Change

- `run_checks` reads HEAD and dirtiness again after the last command. If
  either differs from the pre-run sample, the report's `dirty` is forced to
  true and a `CheckResult` with command `<worktree changed during run>` and
  exit 1 is appended, so `ready` refuses with the existing "run loopzero check
  at this head" reason.
- `cmd_review` reads HEAD and dirtiness after `runners.review_with` returns
  and before `_save_review`. On drift it raises `CliError` naming both SHAs
  and does not post. The reviewer run is lost, which is acceptable because
  it did not consume the on-PR budget.

## Tests

- Fake check command commits into the worktree; report is not ok and names
  the drift.
- Fake reviewer touches a file; `review` refuses to post and nothing is saved.

## Cost

15–25 lines. No new state, no lock file.
