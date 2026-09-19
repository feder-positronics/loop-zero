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
| `loopzero start <slug>` | Create a worktree and branch from base; write the Context/Problem/Goal task template. |
| `loopzero check` | Run `workflow.toml` checks in a bwrap sandbox; exit code is the verdict. |
| `loopzero pr` | Push and open a draft PR from the task file, with its Validation section rendered. |
| `loopzero review` | Run one Claude or Codex review on the exact head; post it and append its summary under Review. |
| `loopzero ready` | Verify head, findings and CI; mark the PR ready for review. |
| `loopzero merge` | Recheck readiness, merge with the configured strategy, remove branch and worktree. |

`loopzero status` prints where the current branch is in that sequence.

For agent-directed delegation, [models.toml](models.toml) holds the current
provider, model, effort, and category defaults. Agents read it through the
shared [model selection and delegation guidance](docs/DELEGATION.md), check
runtime support, and explicitly select the delegate's model and effort.

After creation, `check` updates only the generated checks block in the live PR's
Validation section. Human validation notes and review summaries survive refreshes.
Check timings remain in terminal output, so duration changes alone do not rewrite
the PR and retrigger policy CI. Keep human evidence outside the checks markers.

Running `pr` again explicitly publishes task.md's title and narrative sections.
The live PR owns Validation and Review, and sections absent from task.md are kept.
Edit validation evidence on GitHub after creation; keep acceptance changes in
both task.md (the reviewer's task context) and the PR by rerunning `pr`.

GitHub reviews include at most the last 30,000 UTF-8 bytes of raw reviewer output,
with a truncation notice for longer transcripts. Verdicts and findings remain
complete. The full transcript stays in the saved local
`.loopzero/review-<head>-<kind>.json` artifact; `loopzero review --repost` uses that
artifact without rerunning the model.

## Adopt it in six steps

1. Install from a pinned SHA: `uv tool install git+https://github.com/feder-positronics/loop-zero@<sha>`.
2. Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` at
   your repo root and set `repo.name`, `checks.commands` and `required_ci`.
3. Make sure `git`, `gh auth status`, `bwrap` and `claude` or `codex` work in
   your shell (details in [SETUP.md](SETUP.md)).
4. Point your agents at the skills in [core/skills](core/skills): `plan`,
   `implement`, `review`, `security-review`, `diagnose`, and `typesafe-ai` when
   needed. Follow [agent setup](SETUP.md#connect-your-agent) using a source
   checkout at the same pinned revision as the CLI.
5. For integrations that need API keys, follow
   [credential setup](SETUP.md#api-keys-for-local-agents). TypeSafe uses
   `TYPESAFE_API_KEY` or `~/.config/typesafe/api-key`; no key is needed for
   loop-zero's ordinary checks.
6. Run `loopzero start first-task`, make a change, and walk the six commands.

## Configuration

Delivery configuration lives in one file, `workflow.toml`, at the repository root
(start from [workflow.example.toml](workflow.example.toml)). This repository's
own [workflow.toml](workflow.toml) is a working example.

For API keys used during agent development, follow the
[credential convention](core/CREDENTIALS.md): provider environment variable
first, then a private local file outside the repository. See
[setup](SETUP.md#api-keys-for-local-agents) for provisioning, Bitwarden use,
rotation, and CI/production guidance. Keep secret values out of `workflow.toml`.

| Key | Meaning |
| --- | --- |
| `repo.name` | GitHub repository as `owner/name`; used for every `gh` call. |
| `repo.base` | Branch that `start` branches from and `merge` merges into. |
| `checks.commands` | Shell commands `check` runs in order inside the sandbox; the first nonzero exit fails the run. |
| `checks.required_ci` | Exact GitHub check-run or commit-status names required on the head before `ready` and `merge`; every signal with a required name must succeed. |
| `checks.ro_paths` | Absolute host paths mounted read-only into the sandbox. `/home` is hidden, so list every toolchain path under it (for example `~/.local/bin` and `~/.local/share/uv`, spelled out); put writable caches in `checks.writable`. |
| `checks.writable` | Absolute host paths mounted read-write into the sandbox, such as a shared `~/.cache/uv`; this is a trust decision and the forbidden-path rules from `checks.ro_paths` apply. |
| `checks.scratch` | Worktree-relative directories given a fresh writable mount per run; defaults to `.venv`, `.ruff_cache`, `.pytest_cache` and `node_modules/.cache`. |
| `checks.env` | Fixed environment variables that override sandbox defaults and host values; `PATH` and `HOME` cannot be set. |
| `checks.network` | Allow network inside the sandbox; default `false`. |
| `checks.env_allowlist` | Environment variables passed into the sandbox; default `PATH HOME LANG LC_ALL TERM`. |
| `delivery.merge` | Strategy for `gh pr merge`: `squash`, `merge`, `rebase`, or `queue` when the base branch has a merge queue that owns the method; `merge` then enqueues and must be rerun to verify. |
| `delivery.reviewers` | Reviewer families in order of preference (`claude`, `codex`); when the author family is known, `review` only uses a different family. |
| `delivery.reviewer_ro_paths` | Absolute reviewer CLI/runtime paths mounted read-only. Defaults to the resolved directory of the selected reviewer binary; list an npm or bun prefix for Claude when needed. |

## What it deliberately does not do

- No ledger, evidence store or run log. The PR is the record.
- No scheduler or daemon. Every command is invoked by you or your agent.
- No dashboard. `loopzero status` and GitHub are the UI.
- No compatibility mode. There is one procedure, the current one.
- No signed evidence or attestations. Checks exit zero or they do not.
- No retry framework. A failed command prints its tail and stops.
- No waivers or override files. Fix the finding or resolve the thread with a
  reason.
- Reviews run with network access because the model CLIs require it. The
  sandbox limits what they can read, but cannot limit what allowed data they send.

## Layout

- [core/CONTRACT.md](core/CONTRACT.md) — the rules, under 80 lines.
- [core/HANDOFF.md](core/HANDOFF.md) — the task file and PR body template.
- [core/skills/](core/skills) — five agent skills.
- [SETUP.md](SETUP.md) — install, configure, first run.
- [docs/REWRITE-SPEC.md](docs/REWRITE-SPEC.md) — module map and shared types
  for contributors.

## License

MIT.

Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
