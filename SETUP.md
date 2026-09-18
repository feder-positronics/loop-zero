# Setup

## Prerequisites

- `gh` logged in to the account that will open and merge PRs:
  `gh auth status` must succeed and have `repo` scope.
- `bwrap` (bubblewrap) on `PATH`. Debian/Ubuntu: `apt install bubblewrap`.
  Fedora: `dnf install bubblewrap`. Without it `loopzero check` refuses to run.
- At least one reviewer CLI logged in: `claude` (`claude -p "hi"` answers) or
  `codex` (`codex exec "hi"` answers). Install both if you want the reviewer
  to be a different family from the author.
- `git` 2.40+, Python 3.14 and [uv](https://docs.astral.sh/uv/).

## Install

Pin to a full commit SHA and upgrade by re-running with a new SHA. As a
user-wide tool:

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
repository root and edit it. Three fields matter most:

- `[repo] name` — `owner/name` as GitHub knows it.
- `[checks] commands` — what must exit zero before a PR opens. Start with
  your test and lint commands; add more only when they catch real defects.
- `[checks] required_ci` — exact GitHub check names that must be green before
  merge. Match them to your branch protection rule.

Commit `workflow.toml`; it is the only configuration file.

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

`loopzero status` at any point tells you which step is next and why the
previous one is or is not satisfied.
