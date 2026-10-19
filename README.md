# loop-zero

loop-zero is a small command-line toolkit that takes a coding change from
"start" to "merged" in six explicit steps: worktree, sandboxed checks, draft
PR, one model review (independent by default), readiness, merge. Git, GitHub and
CI are the delivery authority. Local task context, check receipts and saved
review output are disposable working artifacts; the PR remains the delivery
record.

It exists for people and coding agents who ship many small PRs and want the
same discipline on every one of them: checks run in a sandbox with read-only
Git metadata and no network by default, a reviewer reads the exact head (from a
different family by default, subject to the contract's bounded owner-approved
contributor-review exception), and a PR merges only when the head is unchanged since review, no
blocking finding is open and required CI is green. The full rules fit in
[core/CONTRACT.md](core/CONTRACT.md).

The [architecture guide](docs/ARCHITECTURE.md) explains current boundaries,
ownership and the contributor routine that keeps documentation current.

## The six commands

| Command | What it does |
| --- | --- |
| `loopzero start <slug>` | Create a worktree and branch from base; write the Context/Problem/Goal task template. |
| `loopzero check` | Run `workflow.toml` checks in a bwrap sandbox; exit code is the verdict. |
| `loopzero pr` | Push and open a draft PR from the task file, with its Validation section rendered. |
| `loopzero review` | Run one Claude or Codex review on the exact head; post it and append its summary under Review. Exits 0 on approval, 5 on changes requested. |
| `loopzero ready [--wait[=SECONDS]]` | Verify head, findings and CI; optionally wait for required checks on that head. |
| `loopzero merge [--wait[=SECONDS]]` | Recheck readiness and merge; optionally wait for required checks and landing before cleanup. |

`loopzero status` prints where the current branch is in that sequence.
`loopzero resolve` lists the open blocking findings with their ids;
`loopzero resolve <id> "<what changed>"` replies in that thread and resolves it.

For agent delegation, follow [model selection](docs/DELEGATION.md) using the
task repository's [models.toml](models.toml).

PR narrative and generated evidence follow the [contract](core/CONTRACT.md#the-six-steps).
Check timings stay in terminal output; duration changes alone do not rewrite the
PR or retrigger policy CI. Keep human evidence outside the generated checks markers.
`pr` leaves the PR title unchanged. After creation, edit validation evidence on
GitHub; keep acceptance changes in both task.md and the PR by rerunning `pr`.

Review publication records duration and distinguishes requested model/effort from
CLI-reported claims, which are not backend attestation; saved artifacts preserve
them for `--repost`. GitHub reviews include at most the last 30,000 UTF-8 bytes of
raw reviewer output, with a truncation notice. Verdicts and findings remain
complete. The full transcript stays in `.loopzero/review-<head>-<kind>.json`;
`loopzero review --repost` uses it without rerunning the model.

Waits observe immediately, then use 30/60/120-second intervals with up to 10%
earlier jitter, bounded by the timeout. Each readiness observation rechecks
reviews and findings. Mergify waits use REST for source-head guards, merge
observation, queue comments, PR lookup/detail and draft creation; native GitHub
queue membership and review threads use GraphQL. With `ready --wait`, a failed or
cancelled required Actions check may trigger a lookup for a newer active run on
that head; fine-grained tokens need Actions read permission.

Quota handling follows the contract's [failure rules](core/CONTRACT.md#failure):
a GitHub rate-limit error stops the command without retry. REST and GraphQL have
separate primary budgets, shared by all sessions of one user. For an explicit
primary GraphQL limit, loopzero may make one bounded diagnostic query for a
verified reset time; it does not preflight requests or infer GraphQL capacity
from REST `/rate_limit`. See [GitHub's rate-limit guidance](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).

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

Delivery configuration lives in one file, `workflow.toml`, at the repository
root. The annotated [workflow.example.toml](workflow.example.toml) documents its
keys; this repository's own [workflow.toml](workflow.toml) is a working example.
Keep secret values out of it; API keys follow the
[credential convention](core/CREDENTIALS.md) and [setup](SETUP.md#api-keys-for-local-agents).

Settings and semantics the example does not spell out:

| Key | Meaning |
| --- | --- |
| `checks.required_ci` | Names may be check runs or commit statuses; for each required name, the newest check run and the newest commit status, whichever exist, must succeed. |
| `checks.writable` | The forbidden-path rules from `checks.ro_paths` apply. |
| `checks.limits` | Per-check resource limits applied with `prlimit`, capped at the inherited hard limits: `memory_mb = 4096` (address space) and `file_mb = 2048` (file size); overrides must be positive integers. |
| `checks.env_allowlist` | Environment variables passed into the sandbox; default `PATH HOME LANG LC_ALL TERM`. |
| `delivery.review_chunk_bytes` | Diff chunk budget in UTF-8 bytes, at least `4096`; default `200000`. Oversized file patches split by lines; large deleted files are summarized. |

## What it deliberately does not do

- No ledger, evidence store or run log. The PR is the record.
- No scheduler or daemon. Every command is invoked by you or your agent.
- No dashboard. `loopzero status` and GitHub are the UI.
- No compatibility mode. There is one procedure, the current one.
- No signed evidence or attestations. Checks exit zero or they do not.
- No waivers or override files. Fix the finding or resolve the thread with a
  reason.
- Reviews run with network access because the model CLIs require it. The
  sandbox limits what they can read, but cannot limit what allowed data they send.

## Layout

- [core/CONTRACT.md](core/CONTRACT.md) — the delivery rules.
- [core/HANDOFF.md](core/HANDOFF.md) — the task file and PR body template.
- [core/skills/](core/skills) — core agent skills and their routing.
- [integrations/](integrations/) — optional vendor integration skills.
- [SETUP.md](SETUP.md) — install, configure, first run.
- [docs/REWRITE-SPEC.md](docs/REWRITE-SPEC.md) — contributor module map.

## License

MIT.

Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
