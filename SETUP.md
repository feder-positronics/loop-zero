# Setup

## Prerequisites

- `git` 2.40+, Python 3.14, [uv](https://docs.astral.sh/uv/).
- `gh` logged in with `repo` scope; `bwrap` on `PATH` (`loopzero check`
  refuses to run without it).
- `claude` or `codex` CLI logged in; both if the reviewer must differ from
  the author.
- On CI or any unattended host give Claude a non-rotating API key or setup
  token; interactive OAuth rotates its token and is revoked on the second run.

## Install

Pin to a full commit SHA; upgrade by re-running with a new SHA. User-wide:

```sh
uv tool install "git+https://github.com/feder-positronics/loop-zero@<sha>"
```

As a dev dependency of your repository:

```sh
uv add --dev "loopzero @ git+https://github.com/feder-positronics/loop-zero@<sha>"
```

## Configure

Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` in the
repository root and set `[repo] name`. Then:

- `[checks] commands` — must exit zero before a PR opens; for uv projects
  `uv run --group dev ruff check .` and `uv run --group dev pytest -q` work.
- `[checks] required_ci` — exact GitHub check names green before merge.
- `[checks] ro_paths` — `/home` is hidden; list toolchain paths under it in
  full (`/home/<you>/.local/bin`, `/home/<you>/.local/share/uv`).
- `[checks] writable` — the worktree is read-only except per-run scratch over
  `[checks] scratch` (default `.venv`, `.ruff_cache`, `.pytest_cache`, created
  as empty git-invisible dirs). List a shared cache like `~/.cache/uv` here; a
  check can then write to it (trust). uv projects must commit `uv.lock`.
- `[checks] env` — e.g. `{ UV_CACHE_DIR = "/home/<you>/.cache/uv" }`; wins over
  sandbox defaults and host variables. `PATH` and `HOME` cannot be overridden.
- `[delivery] reviewer_ro_paths` — optional absolute reviewer CLI/runtime
  paths. The default is the selected binary's resolved directory; Claude may
  need its npm or bun prefix listed.
- The first `check` in a fresh worktree needs `network = true` or a warm uv
  cache in `writable`: warm it once by running your check commands on the
  host (for uv, `uv sync --group dev`), which also fetches build backends. `PYTHONDONTWRITEBYTECODE`, `RUFF_CACHE_DIR`,
  `UV_CACHE_DIR`, `PYTEST_ADDOPTS` are preset in the sandbox.

## First run

```sh
loopzero start hello-loopzero        # new worktree + branch; cd to the printed path
$EDITOR .loopzero/task.md             # fill Context, Problem, Goal and Acceptance
# ...make a small change and add a test...
loopzero check                        # exit 0, or FAIL; renders the PR Validation section when open
loopzero pr                           # draft PR opens; URL printed
loopzero review                       # model review posted on the PR
loopzero check && loopzero review     # after fixing blocking threads: delta review
loopzero ready                        # marks PR ready if head/findings/CI pass
loopzero merge                        # merges, deletes branch and worktree
loopzero status                       # at any point: next step and why
```

## Merging under a merge queue

When the base branch uses a GitHub merge queue, `loopzero merge` enqueues the
PR and exits after printing that it is queued. Nothing polls. Run
`loopzero merge` again once the queue has landed it; that run verifies the
merge commit, deletes the remote branch and removes the worktree. The
repository must allow auto-merge (Settings, General, "Allow auto-merge"),
otherwise `gh pr merge` fails with "Auto merge is not allowed". Set
`delivery.merge = "queue"` so the queue owns the merge method.
