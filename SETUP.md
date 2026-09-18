# Setup

## Prerequisites

- `gh` logged in with `repo` scope; `bwrap` on `PATH` (`apt`/`dnf install
  bubblewrap`), without which `loopzero check` refuses to run.
- A reviewer CLI logged in: `claude` (`claude -p hi` answers) or `codex`
  (`codex exec hi` answers); both if the reviewer must differ from the author.
- On CI or any unattended host give Claude a non-rotating API key or setup
  token: interactive OAuth rotates its refresh token and the stored login is
  revoked on the second unattended run.
- `git` 2.40+, Python 3.14 and [uv](https://docs.astral.sh/uv/).

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

- `[checks] commands` — must exit zero before a PR opens. For a uv project
  `uv run --group dev ruff check .` and `uv run --group dev pytest -q` work
  out of the box.
- `[checks] required_ci` — exact GitHub check names green before merge.
- `[checks] ro_paths` — `/home` is hidden; list toolchain paths under it in
  full (`/home/<you>/.local/bin`, `/home/<you>/.local/share/uv`).
- `[checks] writable` — the worktree is read-only except per-run scratch over
  `[checks] scratch` (default `.venv`, `.ruff_cache`, `.pytest_cache`). List a
  shared cache like `~/.cache/uv` here; a check can then write to it (trust).
- The first `check` in a fresh worktree needs `network = true` or a warm uv
  cache in `writable`. `PYTHONDONTWRITEBYTECODE`, `RUFF_CACHE_DIR`,
  `UV_CACHE_DIR`, `PYTEST_ADDOPTS` are preset in the sandbox.

## First run

```sh
loopzero start hello-loopzero        # new worktree + branch; cd to the printed path
$EDITOR .loopzero/task.md             # fill Objective and Acceptance
# ...make a small change and add a test...
loopzero check                        # exit 0, or FAIL with the failing tails; report in .loopzero/checks.json
loopzero pr                           # draft PR opens; URL printed
loopzero review                       # model review posted on the PR
# fix critical/important threads, push, then:
loopzero check && loopzero review     # delta review of the new commits
loopzero ready                        # marks PR ready if head/findings/CI pass
loopzero merge                        # merges, deletes branch and worktree
loopzero status                       # at any point: next step and why
```
