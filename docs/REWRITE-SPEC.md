# loop-zero v2: minimal rewrite spec

Date: 2026-09-18. Owner: Marcin. Coordinator: this branch `v2/minimal-rewrite`.

## Goal

Replace the 67k-line workflow engine with a small, boring toolkit. Git, GitHub
and CI are the state. There is no ledger, no authority store, no run log, no
generations/slots/leases, no provisional bindings, no settlement capsules, no
compatibility mode for old runs. The old tree exists only in git history
(`git show main:<path>`) and may be read for facts, never copied wholesale.

Budget: `src/` <= 3,700 lines, `tests/` <= 4,600 lines, `core/` <= 600 lines
of Markdown, one CI workflow. Anything that pushes past the budget needs a
reason in the PR. CI compares each candidate with its base: over the cap, a
change may still merge if it holds or shrinks the count, so an over-budget
main never blocks its own repair. The caps were set to the measured counts on
2026-09-18 after concurrent merges overshot the original 2,500/3,500, then
raised by 40/50 for merge-queue handling in `merge` the same day. Later raises
to 3,000/3,800 left no headroom: by 2026-09-19 both counts sat exactly on the
caps, #163 deleted a working resolver and #165 consolidated tests to fit 11
lines. On 2026-09-20 the owner approved one budgeted raise to 3,400/4,300 to
fund the CLI fixes filed as #166-#175 (#169). The caps live only in
`scripts/size_budget.sh`, which CI and `loopzero check` both run and which
prints the remaining headroom. A further raise needs an issue naming what it
pays for. Issue #196 funds the shared Mergify provider: the first slice raises
the caps to 3,700/4,600 for explicit admission, API credentials and membership,
bounded queue waiting and their failure/security tests. No existing behavior or
check is removed to make room. Candidate eligibility is a separate measured slice.

## The delivery procedure (the whole contract)

1. `loopzero start <task-slug>` — create an owned worktree and branch from the
   current base; write `.loopzero/task.md` (objective, acceptance, base SHA).
2. Implement. `loopzero check` runs the consumer's declared commands inside a
   sandbox: read-only source, no Git write access, isolated HOME, no network
   unless declared. Exit code is the verdict. No signed artifacts.
3. `loopzero pr` — push and open a **draft** PR whose body is generated from
   `.loopzero/task.md` plus the check summary. Drafts never need clean reviews.
4. `loopzero review` — run one independent model review (Claude or Codex,
   different family from the author when known) on the exact head. Findings
   are posted as a GitHub PR review with inline comments. At most one primary
   review plus one delta review per head lineage; the delta reviews only the
   diff since the reviewed commit. Blocking = `critical`/`important`.
5. `loopzero ready` — computes readiness: head unchanged since review, no open
   blocking findings (unresolved review threads whose body carries the marker
   `<!-- loopzero:finding severity=important|critical -->`), required CI
   checks green on exact head. If ready, marks the PR ready for review.
6. `loopzero merge` — re-runs the readiness computation, merges via `gh` with
   the consumer's configured strategy, verifies the merge SHA, deletes the
   branch and the worktree. Nothing else is recorded anywhere.

`loopzero status` prints the current state derived from Git + GitHub only.

## Module map (disjoint ownership for parallel work)

| Module | Owner task | Responsibility |
| --- | --- | --- |
| `src/loopzero/config.py` | sandbox | Load `workflow.toml` → frozen `Config` dataclass (below). |
| `src/loopzero/sandbox.py` | sandbox | `run_checks(config, worktree) -> CheckReport`; bwrap wrapper; graceful `SandboxUnavailable` when bwrap cannot run. |
| `src/loopzero/runners.py` | runners | `review_with(model_family, prompt, cwd) -> ReviewResult`; thin subprocess adapters for `claude -p` and `codex exec`; JSON schema for findings; family detection from a commit trailer or env. |
| `src/loopzero/github.py` | github | `gh`-CLI wrapper: `create_draft_pr`, `post_review`, `open_blocking_findings`, `check_runs`, `mark_ready`, `merge`, `pr_for_branch`. All via `gh api`/`gh pr`; no PyGithub. |
| `src/loopzero/worktree.py` | github | `start`, `cleanup`, `head`, `base`, `is_dirty`, `task_file` helpers. |
| `src/loopzero/cli.py` | cli (wave 2) | argparse; composes the modules into the six commands. |
| `core/CONTRACT.md`, `core/skills/*`, `README.md`, `SETUP.md`, `workflow.example.toml` | docs | The human-facing contract, five skills (plan, implement, review, security-review, diagnose), adoption guide. |

## Shared types (put in `src/loopzero/types.py`; owner: sandbox task creates it first, others import)

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Config:
    repo: str                      # "owner/name"
    base_branch: str               # "main"
    checks: tuple[str, ...]        # shell commands run in sandbox, in order
    required_ci: tuple[str, ...]   # exact GitHub check names that must be green
    merge_strategy: str            # "squash" | "merge" | "rebase"
    reviewers: tuple[str, ...]     # ordered preference: ("claude", "codex")
    reviewer_ro_paths: tuple[str, ...] = ()  # reviewer CLI/runtime paths
    network: bool = False          # allow network inside sandbox
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")

@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    duration_s: float
    tail: str                      # last 40 lines of combined output

@dataclass(frozen=True)
class CheckReport:
    head: str
    dirty: bool
    results: tuple[CheckResult, ...]
    @property
    def ok(self) -> bool: ...

@dataclass(frozen=True)
class Finding:
    severity: str                  # "critical" | "important" | "suggestion"
    path: str | None
    line: int | None
    title: str
    body: str

@dataclass(frozen=True)
class ReviewResult:
    family: str                    # "claude" | "codex"
    head: str
    kind: str                      # "primary" | "delta"
    verdict: str                   # "approve" | "request_changes"
    findings: tuple[Finding, ...]
    raw: str                       # untouched model output for the PR comment
```

`workflow.toml` shape:

```toml
[repo]
name = "owner/name"
base = "main"

[checks]
commands = ["uv run pytest -q", "uv run ruff check ."]
required_ci = ["checks"]
network = false

[delivery]
merge = "squash"
reviewers = ["claude", "codex"]
# Optional; defaults to the selected reviewer's resolved binary directory.
reviewer_ro_paths = ["/absolute/reviewer/runtime/path"]
```

## Rules for every module

- Python 3.14, stdlib only in `src/`. Tests use pytest and fake executables
  placed on `PATH` by fixtures (fake `gh`, fake `claude`, fake `codex`,
  fake `bwrap`). No network, no real credentials in tests.
- Every subprocess call goes through one helper `loopzero._proc.run(argv, cwd,
  env, timeout)` that strips the environment to the allowlist. Sandbox owner
  creates `_proc.py`; others import it.
- Fail closed and loud: a missing tool or a nonzero exit raises a typed error
  with the command and the tail of its output. Never retry automatically.
- No global state, no files outside the worktree except what `gh` needs.
- Each module ships with tests in `tests/test_<module>.py`. Use tmp git repos
  created in fixtures for anything touching Git.
- Docstrings say what, not why the old system did something else. Do not
  reference intelflo, ledgers, authority, generations or settlement anywhere.
