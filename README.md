# loop-zero

loop-zero is a small command-line toolkit that takes a coding change from
"start" to "merged" in six explicit steps: worktree, sandboxed checks, draft
PR, one independent model review, readiness, merge. Git, GitHub and CI are the
only state. Nothing is written anywhere else, so there is nothing to sync,
migrate or clean up.

It exists for people and coding agents who ship many small PRs and want the
same discipline on every one of them: checks run in a sandbox that cannot touch
Git or the network, a reviewer from a different model family reads the exact
head, and a PR merges only when the head is unchanged since review, no blocking
finding is open and required CI is green. The full rules fit in
[core/CONTRACT.md](core/CONTRACT.md).

## The six commands

| Command | What it does |
| --- | --- |
| `loopzero start <slug>` | Create a worktree and branch from base; write `.loopzero/task.md`. |
| `loopzero check` | Run `workflow.toml` checks in a bwrap sandbox; exit code is the verdict. |
| `loopzero pr` | Push and open a draft PR whose body is the task file plus check summary. |
| `loopzero review` | Run one Claude or Codex review on the exact head; post it as a PR review. |
| `loopzero ready` | Verify head, findings and CI; mark the PR ready for review. |
| `loopzero merge` | Recheck readiness, merge with the configured strategy, remove branch and worktree. |

`loopzero status` prints where the current branch is in that sequence.

## Adopt it in five steps

1. Install from a pinned SHA: `uv tool install git+https://github.com/feder-positronics/loop-zero@<sha>`.
2. Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` at
   your repo root and set `repo.name`, `checks.commands` and `required_ci`.
3. Make sure `git`, `gh auth status`, `bwrap` and `claude` or `codex` work in
   your shell (details in [SETUP.md](SETUP.md)).
4. Point your agents at the skills in [core/skills](core/skills): `plan`,
   `implement`, `review`, `security-review`, `diagnose`.
5. Run `loopzero start first-task`, make a change, and walk the six commands.

## What it deliberately does not do

- No ledger, evidence store or run log. The PR is the record.
- No scheduler or daemon. Every command is invoked by you or your agent.
- No dashboard. `loopzero status` and GitHub are the UI.
- No compatibility mode. There is one procedure, the current one.
- No signed evidence or attestations. Checks exit zero or they do not.
- No retry framework. A failed command prints its tail and stops.
- No waivers or override files. Fix the finding or resolve the thread with a
  reason.

## Layout

- [core/CONTRACT.md](core/CONTRACT.md) — the rules, under 80 lines.
- [core/HANDOFF.md](core/HANDOFF.md) — the task file and PR body template.
- [core/skills/](core/skills) — five agent skills.
- [SETUP.md](SETUP.md) — install, configure, first run.
- [docs/REWRITE-SPEC.md](docs/REWRITE-SPEC.md) — module map and shared types
  for contributors.

## License

MIT.
