# Contributor module map

The rewrite is implemented. This map describes the current module boundaries;
delivery behavior belongs in the live documents linked below.

## Modules

| Module in `src/loopzero/` | Responsibility |
| --- | --- |
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

## Live references

- [Delivery contract](../core/CONTRACT.md): readiness, review budget, sandbox
  boundaries, failures and merge cleanup.
- [Skill catalog](../core/skills/README.md): planning, implementation and review
  responsibilities.
- [README configuration](../README.md#configuration), [setup](../SETUP.md) and
  [example workflow](../workflow.example.toml): supported keys and first run.
- [Mergify adoption](MERGIFY.md): queue deployment, candidate proof and rollback.
- [Architecture guide](ARCHITECTURE.md): current system boundaries, ownership
  and the dated decision notes behind them.
- [Size budget script](../scripts/size_budget.sh): authoritative source/test
  caps and comparison with base; over-cap changes pass only when they do not grow.
- [Tests](../tests/): module behavior proofs using temporary Git repositories
  and fake executables, without network or real credentials.
