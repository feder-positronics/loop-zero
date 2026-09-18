# Validate the PR base and finish remote branch cleanup

Date: 2026-09-18. Status: proposed. Two narrow fixes with one theme: the PR
on GitHub can differ from what the local commands assumed.

## Failure 1: retargeted PR

`cli._primary_base` computes the review diff against `origin/<repo.base>`.
`github.merge` merges whatever base the PR currently has, protected only by
`--match-head-commit`. A PR retargeted on GitHub after review is merged into
a branch the review never considered.

### Change

`github.readiness` receives the configured base branch and adds the reason
`PR targets <base_ref>, configured base is <base>` when they differ. `ready`,
`merge` and `status` inherit it. `review` refuses early with the same message.

## Failure 2: remote branch left behind

`github.merge` merges, verifies, then deletes the remote branch. If the
delete fails for a reason other than "already gone", `MergeFailed` propagates
and `cmd_merge` exits nonzero after a successful merge. Rerunning `merge`
takes the already-merged path, which only removes the local worktree. The
remote branch stays.

### Change

Split `github.merge` into `merge` (merge and verify) and
`delete_remote_branch`. `cmd_merge` calls the second in both the fresh-merge
and already-merged paths, and a failure there prints a warning with the
branch name instead of raising, matching how local cleanup failure is already
reported.

## Tests

- PR JSON with `baseRefName = "release"` and config base `main`: `ready`
  lists the reason; `review` refuses.
- Fake `gh` fails the ref delete once; second `merge` run deletes it.

## Cost

15–25 lines across `github.py` and `cli.py`, three tests.
