# Contributor module map

Current module boundaries. The [architecture guide](ARCHITECTURE.md) explains
the system boundaries; the [contract](../core/CONTRACT.md) owns delivery behavior.

## Modules

| Module in `src/loopzero/` | Responsibility |
| --- | --- |
| `_cli_entry.py` | Console entry: parse arguments before loading commands. |
| `_cli_args.py` | Define the command-line parser and options. |
| `types.py` | Shared configuration, check reports, findings and review results. |
| `config.py` | Validate `workflow.toml`; load trusted base sections. |
| `_proc.py` | Run subprocesses with an environment allowlist, timeouts and typed failures. |
| `sandbox.py` | Build bwrap mounts, run checks with resource limits, detect worktree drift. |
| `runners.py` | Run Claude or Codex in bwrap, parse results, chunk diffs, detect author families from `Co-Authored-By` trailers across the PR's `base..HEAD` lineage. |
| `github.py` | GitHub CLI/API access, PR creation and body writes, review publication and threads, readiness and merge operations. |
| `worktree.py` | Create task branches and worktrees, read Git/task context, clean up. |
| `cli.py` | Compose the delivery commands, PR body sections, check receipts, review lineage and bounded waits. |
| `eligibility.py` | Evaluate authored-head review eligibility and publish its status. |
| `hosted.py` | Dispatch trusted hosted eligibility refreshes from GitHub events. |
| `mergify.py` | Read queue membership and configuration; request explicit admission or removal. |
| `candidate.py` | Attest integration candidates against live sources and trusted review evidence. |

[tach.toml](../tach.toml) holds the dependency layers between these modules,
enforced by `tach check` in CI and `loopzero check`.
[scripts/size_budget.sh](../scripts/size_budget.sh) holds the source, test and
Markdown line caps; Ruff's `C901` rule in [pyproject.toml](../pyproject.toml)
checks per-function complexity.
