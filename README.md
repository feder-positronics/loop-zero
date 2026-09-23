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
| `loopzero review` | Run one Claude or Codex review on the exact head; post it and append its summary under Review. Exits 0 on approval, 5 on changes requested. |
| `loopzero ready [--wait[=SECONDS]]` | Verify head, findings and CI; optionally wait for required checks on that head. |
| `loopzero merge [--wait[=SECONDS]]` | Recheck readiness and merge; optionally wait for a merge queue before cleanup. |

`loopzero status` prints where the current branch is in that sequence.
`loopzero resolve` lists the open blocking findings with their ids;
`loopzero resolve <id> "<what changed>"` replies in that thread and resolves it.

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

Wait commands observe immediately, then back off to 30, 60 and 120 second intervals
(with up to 10% earlier jitter), bounded by the remaining wait timeout. Readiness
still checks current reviews and findings on every observation. Mergify wait ticks
use REST for both source-head guards and verified merge observation, avoiding
recurring GraphQL PR reads. Native GitHub queue membership still requires GraphQL.
With `ready --wait`, a failed or cancelled required GitHub Actions check may
trigger a workflow-run lookup to see whether a newer run on the same head is
still active. Fine-grained GitHub tokens need Actions read permission for that
lookup.

A GitHub rate-limit error stops the command immediately instead of using transient
5/20/60-second retries. Resume after the primary reset or secondary `Retry-After`
period; loopzero does not schedule a retry. REST and GraphQL have separate primary
budgets, and all sessions using the same user share that user's allowance. See
[GitHub's rate-limit guidance](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).

## Adopt it in six steps

1. Install from a pinned SHA: `uv tool install git+https://github.com/feder-positronics/loop-zero@<sha>`.
2. Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` at
   your repo root and set `repo.name`, `checks.commands` and `required_ci`.
3. Make sure `git`, `gh auth status`, `bwrap` and `claude` or `codex` work in
   your shell (details in [SETUP.md](SETUP.md)).
4. Point your agents at the four entry skills in [core/skills](core/skills):
   `plan`, `implement`, `work-issue`, and `write-design-doc`. Use the catalog's
   methods, including `diagnose`, `review`, and `security-review`, as needed. Optional
   TypeSafe guidance lives in [integrations/typesafe-ai](integrations/typesafe-ai).
   Follow [agent setup](SETUP.md#connect-your-agent) using a source
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
| `delivery.merge` | Strategy for `gh pr merge`: `squash`, `merge`, `rebase`, or `queue` when the base branch has a merge queue that owns the method; use `merge --wait` to enqueue and verify in one run. |
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
- [core/skills/](core/skills) — core agent skills and their routing.
- [integrations/](integrations/) — optional vendor integration skills.
- [SETUP.md](SETUP.md) — install, configure, first run.
- [docs/REWRITE-SPEC.md](docs/REWRITE-SPEC.md) — module map and shared types
  for contributors.

## License

MIT.

Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
