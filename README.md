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
different family by default, with a disclosed same-family fallback when none
can run), and a PR needs an eligible review, resolved
blocking findings and green required CI to merge. The full rules fit in
[core/CONTRACT.md](core/CONTRACT.md).

The [architecture guide](docs/ARCHITECTURE.md) explains current boundaries,
ownership and the contributor routine that keeps documentation current.

## The six commands

| Command | What it does |
| --- | --- |
| `loopzero start <slug>` | Create a worktree and branch from base; write the Goal/Problem/Context task template. |
| `loopzero check` | Run trusted-base checks with local bwrap. Before a long check, open the draft with `loopzero pr` first. |
| `loopzero pr` | Push and open a draft PR from the task file, with its Validation section rendered. |
| `loopzero review` | Run one Claude or Codex review on the exact head; post it and append its summary under Review. Exits 0 on approval, 5 on changes requested. |
| `loopzero ready [--wait[=SECONDS]]` | Verify head, findings and CI; optionally wait for required checks on that head. |
| `loopzero merge [--wait[=SECONDS]]` | Recheck readiness and merge; optionally wait for required checks and landing before cleanup. |

For a picture of the whole flow, see the [workflow diagrams](docs/WORKFLOW.md).

`loopzero status` shows GitHub's draft state on its `pr:` line and, when a PR
exists, the local readiness verdict that `loopzero merge` uses on its `ready:`
line; it can exit 0 with `ready: no`. Queueing directly in GitHub with a red
local check receipt is covered only by the contract's
[owner queue route](core/CONTRACT.md#blockers).
It requires authenticated GitHub access even before a PR exists.
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
them in `.loopzero/review-<head12>-<kind>.json`. `loopzero review --repost` uses
the saved review without a model call and rejects `--model` and `--effort`.

Waits observe immediately, then use 30/60/120-second intervals with up to 10%
earlier jitter, bounded by the timeout. Each readiness observation rechecks
reviews and findings. Mergify waits use REST for source-head guards, merge
observation, queue comments, PR lookup/detail and draft creation; native GitHub
queue membership and review threads use GraphQL. When a required Actions check
is missing, failed or cancelled, waiters can look for an active workflow on
that head. Fine-grained tokens need Actions read permission.

Quota handling follows the contract's [failure rules](core/CONTRACT.md#failure).
Reads can honor a short `Retry-After`, and active waiters can resume after a
verified reset within their deadline. REST and GraphQL have
separate primary budgets, shared by all sessions of one user. For an explicit
primary GraphQL limit, loopzero may make one bounded diagnostic query for a
verified reset time; it does not preflight requests or infer GraphQL capacity
from REST `/rate_limit`. See [GitHub's rate-limit guidance](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).

## Adopt it in six steps

1. Set `sha` to the pinned commit SHA and run
   `uv tool install "git+https://github.com/feder-positronics/loop-zero@$sha"`.
2. Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` at
   your repo root and set `repo.name`, `checks.commands` and `required_ci`.
   Commit and push `workflow.toml` to the configured remote base
   (`origin/main` by default) before starting your first task.
3. Verify `git`, `gh auth status`, and your `claude` or `codex` CLI. Run
   fresh local reviews and default checks on Linux with `bwrap` on `PATH`.
   See [SETUP.md](SETUP.md).
4. Install the [core/skills](core/skills) once per machine from a source
   checkout at the same pinned revision as the CLI
   ([agent setup](SETUP.md#connect-your-agent)). Entry skills are `plan`,
   `implement`, `work-issue` and `write-design-doc`; use the catalog's other
   methods as needed. Repositories keep no skill copies and customise through
   `AGENTS.md` and the docs it links. Optional TypeSafe guidance lives in
   [integrations/typesafe-ai](integrations/typesafe-ai).
5. For integrations that need API keys, follow
   [credential setup](SETUP.md#api-keys-for-local-agents). TypeSafe uses
   `TYPESAFE_API_KEY` or `~/.config/typesafe/api-key`; no key is needed for
   loop-zero's ordinary checks.
6. Run `loopzero start first-task` and `cd` into the worktree path it prints.
   Edit, commit, and run the remaining delivery commands from that worktree.

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
| `checks.limits` | Local per-check resource limits applied with `prlimit`: `memory_mb = 4096` (address space) and `file_mb = 2048` (file size), capped at inherited hard limits. Overrides must be positive integers. |
| `checks.env_allowlist` | Host variables allowed for checks. An explicit list replaces the configured default (`PATH HOME LANG LC_ALL TERM`). The sandbox forwards `PATH LANG LC_ALL TERM` by default and sets `HOME` to `/tmp/home`. To forward another launcher variable, append its name to `[checks].env_allowlist` in the base branch’s `workflow.toml`. |

## What it deliberately does not do

- No dashboard. `loopzero status` and GitHub are the UI.

## Layout

- [core/CONTRACT.md](core/CONTRACT.md) — the delivery rules.
- [core/HANDOFF.md](core/HANDOFF.md) — the task file and PR body template.
- [core/skills/](core/skills) — core agent skills and their routing.
- [integrations/](integrations/) — optional vendor integration skills.
- [SETUP.md](SETUP.md) — install, configure, first run.
- [docs/REWRITE-SPEC.md](docs/REWRITE-SPEC.md) — contributor module map.
- [docs/BACKLOG.md](docs/BACKLOG.md) — a readable backlog: problems, goals,
  ongoing duties and one pinned "Work still to do" summary.
- [docs/CODE-HEALTH.md](docs/CODE-HEALTH.md) — keeping a repository lean:
  recurring audits that report only new findings and never delete on their own.

## License

MIT.

Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
