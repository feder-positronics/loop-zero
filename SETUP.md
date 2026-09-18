# Setup

## Prerequisites

- `gh` logged in with `repo` scope as the account that opens and merges PRs.
- `bwrap` (bubblewrap) on `PATH` (`apt`/`dnf install bubblewrap`); without it
  `loopzero check` refuses to run.
- At least one reviewer CLI logged in: `claude` (`claude -p "hi"` answers) or
  `codex` (`codex exec "hi"` answers). Install both if you want the reviewer
  to be a different family from the author.
- If `loopzero review` runs from CI or any unattended host, give Claude a
  non-rotating API key or setup token: the interactive OAuth login rotates its
  refresh token, so the stored login is revoked on the second unattended run.
- `git` 2.40+, Python 3.14 and [uv](https://docs.astral.sh/uv/).

## Install

Pin to a full commit SHA; upgrade by re-running with a new SHA. User-wide:

```sh
uv tool install "git+https://github.com/feder-positronics/loop-zero@<sha>"
loopzero --help
```

As a dev dependency of your repository:

```sh
uv add --dev "loopzero @ git+https://github.com/feder-positronics/loop-zero@<sha>"
uv run loopzero --help
```

## Configure

Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` in the
repository root. Three fields matter most:

- `[repo] name` — `owner/name` as GitHub knows it.
- `[checks] commands` — what must exit zero before a PR opens; start with
  your test and lint commands.
- `[checks] required_ci` — exact GitHub check names that must be green before
  merge. Match them to your branch protection rule.

## First run

```sh
loopzero start hello-loopzero        # new worktree + branch; cd to the printed path
$EDITOR .loopzero/task.md             # fill Objective and Acceptance
# ...make a small change and add a test...
loopzero check                        # sandboxed checks; fix until exit 0
loopzero pr                           # draft PR opens; URL printed
loopzero review                       # model review posted on the PR
# fix critical/important threads, push, then:
loopzero check && loopzero review     # delta review of the new commits
loopzero ready                        # marks PR ready if head/findings/CI pass
loopzero merge                        # merges, deletes branch and worktree
```

`loopzero status` tells you which step is next and why the previous one is or
is not satisfied.
