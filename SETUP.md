# Setup

## Prerequisites

- `git` 2.40+, Python 3.14, [uv](https://docs.astral.sh/uv/).
- `gh` from GitHub's [package repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md)
  (tested with 2.101.0), logged in with `repo` scope, so system updates keep it
  current. Distribution packages lag: Ubuntu's 2.46.0 lacks
  `gh pr checks --json`, so scripts written for a current `gh` fail on it. Do
  not use the snap: its confinement cannot reach the keyring. Without root,
  unpack the [release](https://github.com/cli/cli/releases) tarball and put
  `bin/gh` in `~/.local/bin` ahead of `/usr/bin`; that copy does not update.
- Keep `gh` and `claude` current. Codex reviews run only on the versions
  allowlisted in `src/loopzero/runners.py`; upgrade Codex with a loop-zero release
  that accepts the new version. Tools that shape the build (`uv`,
  Python, a consumer's Node and package manager) change only with their pin in
  the repository, for example `uv` in `.github/workflows/checks.yml`.
- `bwrap` on `PATH` (`loopzero check` refuses to run without it).
- `claude` or `codex` CLI logged in; both if the reviewer must differ from
  the author.
- On CI or any unattended host give Claude a non-rotating API key or setup
  token; interactive OAuth rotates its token and is revoked on the second run.

## Install

Pin to a full commit SHA; upgrade by re-running with a new SHA.
`loopzero --version` prints the installed revision. Never edit the installed
package: a hotfix is a branch, a PR, and a reinstall from the merged SHA.
User-wide:

```sh
uv tool install "git+https://github.com/feder-positronics/loop-zero@<sha>"
```

As a dev dependency of your repository:

```sh
uv add --dev "loopzero @ git+https://github.com/feder-positronics/loop-zero@<sha>"
```

Installing the CLI does not provision API keys or install agent skills. Continue
with agent setup, repository configuration, and the optional credential setup
below. For an existing installation, keep its working credentials; upgrading
the CLI or skills does not require recreating them.

## Connect your agent

Keep a source checkout at the same pinned revision as the CLI. The Python wheel
contains the CLI package; the source checkout supplies `core/skills`, the shared
contract, credential guidance, and supporting references.

Install the skills once per machine, not per repository: link each directory
under the checkout's `core/skills` into `~/.agents/skills` (Codex) and
`~/.claude/skills` (Claude Code). Link through one stable path that you repoint
when you upgrade the CLI, so skills and CLI move together, and keep the previous
revision for rollback. Never edit the linked checkout. Both harnesses follow a
symlinked skill directory and resolve its relative links from the real files, so
the contract and references stay reachable. Do not also keep copies of these
skills in a repository: both would load.

```sh
CHECKOUT=/path/to/loop-zero-checkout  # pinned to the CLI revision
SRC=~/.local/share/loopzero/source    # stable path; repoint it to upgrade
mkdir -p ~/.local/share/loopzero ~/.agents/skills ~/.claude/skills
ln -sfn "$CHECKOUT" "$SRC.new" && mv -T "$SRC.new" "$SRC"
for d in "$SRC"/core/skills/*/; do
  n=$(basename "$d")
  ln -sfn "$SRC/core/skills/$n" ~/.agents/skills/"$n"
  ln -sfn "$SRC/core/skills/$n" ~/.claude/skills/"$n"
done
```

Repositories customise through documentation only: `AGENTS.md`, including
folder-level `AGENTS.md` files, and the docs it links ("when X, read Y"). Project
docs add project detail; they never relax the contract. Point your agent's
repository instructions at the checkout's `core/skills/README.md` and
`core/CREDENTIALS.md`. Keep those instructions in `AGENTS.md`: Codex and Claude Code (v2.1.277 or
later) both read it directly. Do not add a `CLAUDE.md`, `.claude/CLAUDE.md` or
`CLAUDE.local.md` on the same path; Claude reads any of them instead of
`AGENTS.md`. Claude cannot read `AGENTS.md` directly with third-party providers
such as Bedrock, with telemetry or the `agents-md` plugin disabled, or in the
first session after an install or upgrade; there, use a local, untracked
`CLAUDE.md` containing `@AGENTS.md`. `/memory` lists the loaded files.
For TypeSafe work, explicitly request [typesafe-ai](integrations/typesafe-ai/SKILL.md)
from the optional integrations.

For model routing, copy [models.toml](models.toml) into the consuming repository,
adjust its models and efforts to the available runtimes, and point repository
instructions at the shared [delegation guidance](docs/DELEGATION.md). Formal
`loopzero review` uses reviewer families from `workflow.toml`; `models.toml` does
not change it.

## Configure

Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` in the
repository root and set `[repo] name`. `loopzero check` uses the base branch's
`[checks]` when present: edited commands take effect after merging; `--config`
unpins them. Then:

- `[checks] commands` — must exit zero before a PR opens; for uv projects
  `uv run --group dev ruff check .` and `uv run --group dev pytest -q` work.
- `[checks] required_ci` — exact GitHub check names green before merge.
- `[checks] ro_paths` — `/home` is hidden; list toolchain paths under it in
  full (`/home/<you>/.local/bin`, `/home/<you>/.local/share/uv`).
- `[checks] writable` — the worktree is read-only except per-run scratch over
  `[checks] scratch` (default `.venv`, `.ruff_cache`, `.pytest_cache`,
  `node_modules/.cache`, created as empty git-invisible dirs). List a shared
  cache like `~/.cache/uv` here; a check can then write to it (trust). uv projects
  must commit `uv.lock`.
- `[checks] env` — e.g. `{ UV_CACHE_DIR = "/home/<you>/.cache/uv" }`; wins over
  sandbox defaults and host variables. `PATH` and `HOME` cannot be overridden.
- `[delivery] reviewer_ro_paths` — optional absolute reviewer CLI/runtime paths;
  the [example](workflow.example.toml) states the default and protected-state limits.
- For offline checks, warm the configured writable uv cache on the host with
  `UV_CACHE_DIR=<configured-cache> uv sync --group dev` (use the groups your
  checks require). This also fetches build backends such as hatchling.
  `check` prepares dependencies with `uv sync --offline --locked` in the same
  sandbox before checks when commands use the simple unquoted form
  `uv run [--group NAME ...] TOOL [ARGS ...]`. A committed `uv.lock` is required.
  Compound commands, other uv options and quoted arguments are executed normally
  without dependency preflight. `PYTHONDONTWRITEBYTECODE`, `RUFF_CACHE_DIR`,
  `UV_CACHE_DIR`, `PYTEST_ADDOPTS` are preset in the sandbox.
- Before running checks, simple unquoted direct `python[3[.VERSION]] PATH [ARGS]`
  and `PATH [ARGS]` commands with relative paths containing `/` are checked for
  missing files. Arguments are limited to letters, digits and `_./=-`; shell
  syntax is not interpreted. Assignment-prefixed commands and paths supplied by
  scratch or writable locations are skipped because earlier checks may create them.
  If trusted-base configuration introduced a script
  absent from an older task tree, integrate the current configured base into
  the task branch, resolve conflicts and rerun. Being behind alone is allowed.
  Progress is flushed before dependency preparation and each check.

## API keys for local agents

The [credential convention](core/CREDENTIALS.md) owns lookup, isolation,
rotation and CI/production credentials. Keep the primary development key in
Bitwarden; the file below is an optional plaintext local copy.

To provision a local copy, run this in your own Bash terminal. Set `api_service`
to the documented service directory name and paste the key at the hidden prompt.
The example configures TypeSafe:

```bash
(
  set +x
  umask 077
  api_service=typesafe
  api_dir="$HOME/.config/$api_service"
  # Refuse redirected paths before changing permissions or writing a key.
  test ! -L "$api_dir" && test ! -L "$api_dir/api-key" || exit 1
  mkdir -p "$api_dir" || exit 1
  chmod 700 "$api_dir" || exit 1
  read -rsp "$api_service API key: " api_key || exit 1
  printf '\n'
  test -n "$api_key" || exit 1
  touch "$api_dir/api-key" || exit 1
  chmod 600 "$api_dir/api-key" || exit 1
  printf '%s' "$api_key" > "$api_dir/api-key"
)
```

Verify file presence and permissions without displaying the key (replace
`typesafe` for another service):

```bash
test -r "$HOME/.config/typesafe/api-key" &&
  test -s "$HOME/.config/typesafe/api-key" &&
  stat -c '%a %n' "$HOME/.config/typesafe" "$HOME/.config/typesafe/api-key"
```

On Linux, this should show `700` for the directory and `600` for the file.
It confirms the local setup, not whether the provider accepts the key. To test
authentication, ask the agent to use the integration skill for one live call
with synthetic input and report the result without credentials. For TypeSafe:
"Use typesafe-ai to run one live smoke test with synthetic text."

Rerun the setup to replace a rotated key. Environment variables are inherited at
process launch; exporting one elsewhere cannot update an existing agent.

### TypeSafe credentials (optional)

The [TypeSafe skill](integrations/typesafe-ai/SKILL.md) uses:

| Setting | Value |
| --- | --- |
| Environment variable (first choice) | `TYPESAFE_API_KEY` |
| Local fallback | `~/.config/typesafe/api-key` |
| Setup snippet's service name | `typesafe` |
| API origin | `https://api.typesafe.ai` |

In Bitwarden, store a dedicated development key in a Login item named
`TypeSafe API`, in its password field. Paste that password at the setup prompt
above. Bitwarden CLI users can instead retrieve it into the calling process's
environment; see the [official CLI guide](https://bitwarden.com/help/cli/).
Lock the vault and remove `BW_SESSION` before starting the agent.

### Several subscription accounts (optional)

`scripts/ai-accounts/` rotates Claude and Codex subscription accounts behind the plain
`claude`, `codex` and `loopzero` commands. Install the monitor and its three shims:

```sh
install -m 755 scripts/ai-accounts/ai-accounts ~/.local/bin/
mkdir -p ~/.local/lib/ai-accounts/bin
for n in claude codex loopzero; do ln -sf ~/.local/bin/ai-accounts ~/.local/lib/ai-accounts/bin/$n; done
```

Put the shims first on PATH by making this the last line of `~/.profile` and
`~/.bashrc`, after anything that prepends `~/.local/bin`:
`export PATH="$HOME/.local/lib/ai-accounts/bin:$PATH"`. Point an agent host's binary
path at `~/.local/lib/ai-accounts/bin/claude` (and `codex`). Enable the monitor by copying
the two systemd units to `~/.config/systemd/user/`, then
`systemctl --user enable --now ai-accounts.timer`.

- Claude accounts are long-lived tokens: run `claude setup-token` per account and
  pipe each into `ai-accounts add claude <name>`. Codex accounts are separate
  `CODEX_HOME`s sharing `~/.codex` except `auth.json`: `ai-accounts add codex <name>`,
  then log in as it prints; the monitor refreshes standby Codex logins before they expire.
- Every 5 minutes the monitor probes each account and keeps the best one active:
  Claude prefers Fable and Opus, then Opus only; Codex prefers every model
  available. At 95% of any limit an account counts as spent. `ai-accounts status`
  shows accounts, emails, tiers and resets.
- Claude probes retain each window's utilization (`util`) and reset (`resets`, null
  when absent), including healthy windows. Legacy `reset` and `next_reset` are
  aggregate availability estimates; status labels 7d and 5h resets separately.
  `weekly_used_percent` is the overall 7d usage, null when unknown.
- Only `exec` (including `--identity`) refuses non-owner Claude launches with exit 3
  at >=85% overall weekly or five-hour utilization, or when that account's cached
  status is missing, unreadable or older than 20 minutes. The last 15 weekly points
  are reserved for required reviews and essential decisions; use
  `AI_ACCOUNTS_CLAUDE_OWNER=1` for that work to bypass the guard. No hysteresis is
  stored. Direct T3 launches, `run` and CLI shims remain outside this guard;
  this is not fleet-wide enforcement. Admission does not probe accounts.
- Each shim starts the real CLI on the selected account and keeps the shims first on
  its PATH. Verified helper-managed credentials inherited by a later command can
  rebind; explicit OAuth tokens, API keys and `CODEX_HOME` values remain caller-owned.
  The helper uses opaque account-binding markers to distinguish these cases.
  Codex bindings retain account identity across routine OAuth token refreshes.
  `loopzero` commands are never replayed by the shim. Existing processes keep their
  credentials until their harness safely replaces them; changing the active file
  does not change a running session. Never pin a token in `settings.json` `env`.
- Harness integrations can call `ai-accounts launch <provider> --model <model>` for
  a nonsecret JSON identity, then `ai-accounts exec <provider> --identity <identity>
  -- /absolute/real/executable ...` to pin that exact managed revision. Pass the
  real executable, not another shim. Preparing the current account does not claim
  model eligibility; its native CLI validates ordinary startup. Choosing a replacement
  requires positive model evidence. `ai-accounts exhausted <provider> --identity
  <failed-identity> --model <model>` selects for the unchanged requested model under
  the selection lock. Concurrent stale failures reuse a recent eligible selection.
  Failed model/revision pairs have a five-minute cooldown; unknown availability is
  not treated as usable. These commands report unsupported or unmanaged routes
  without inventing account credentials. The authentication provider determines
  the account pool, even when a harness can use several model providers.
- `ai-accounts run <provider> --model <model> -- ...` can select a replacement after
  a quota failure without repeating the command. Add `--retry-safe` only for a
  caller-proven repeatable command: it permits one retry after the effective
  credential revision changes. Do not use it for interactive streams, unknown
  tool outcomes or publication. `status` marks quota evidence over ten minutes old
  as stale; `status --json` exposes the same flag.
- After host recovery, verify the account timer has a future next run. Switching
  the active account does not update an existing provider process: resume through
  the managed launcher on the selected credential. After failover, reconcile parked
  or failed unfinished jobs; a resent message alone does not prove a new process or
  a completed tool call.
- Token sessions lack claude.ai connectors. Changing an agent host's binary path
  may restart its provider and interrupt running sessions.

## First run

```sh
loopzero start hello-loopzero        # new worktree + branch; cd to the printed path
$EDITOR .loopzero/task.md             # fill Context, Problem, Goal and Acceptance
# ...make a small change and add a test...
git add . && git commit -m "Describe the change"
loopzero check                        # exit 0, or FAIL; renders the PR Validation section when open
loopzero pr                           # draft PR opens; URL printed
loopzero review                       # model review posted on the PR

# If blocking threads need fixes, apply them, then:
git add . && git commit -m "Fix review findings"
loopzero check && loopzero pr         # push the fixes
loopzero resolve <id> "<fix commit and what changed>"  # once per repaired blocking thread
loopzero review                       # delta review of the fixes
loopzero ready --wait                 # marks the PR ready and waits for required CI
loopzero status                       # next step and why
cd <repository-root>
(cd <worktree> && loopzero merge --wait)  # landed merge deletes branch and worktree
```

## Merging under a merge queue

When the base branch uses a GitHub merge queue, `loopzero merge` enqueues the
PR. With `--wait` it follows the queue to the landed commit; without it, it
exits after printing that it is queued, and a later `loopzero merge` verifies the
merge commit, deletes the remote branch and removes the worktree. The
repository must allow auto-merge (Settings, General, "Allow auto-merge"),
otherwise `gh pr merge` fails with "Auto merge is not allowed". Set
`delivery.merge = "queue"` so the queue owns the merge method. On an already
merged PR, rerunning `merge` attempts remote branch and worktree cleanup again;
cleanup failures print warnings.

### Mergify

Set `delivery.merge = "mergify"` and `delivery.mergify_queue` to the exact
Mergify queue-rule name. `loopzero merge` posts the documented
`@mergifyio queue <name>` command once for the current source head, then confirms
membership through `https://api.mergify.com/v1`. A command comment is only a
request; a behind branch is treated as queued only after that API confirms it.

Use draft-PR integration checks: set `merge_queue.max_parallel_checks` greater
than one. In-place updates rewrite the reviewed source
head and are unsupported. Admission checks the live serial queue's identity and
reads `.mergify.yml` from GitHub's default branch, requiring explicit integer
parallelism greater than one and the matching queue name. The configuration blob
must stay unchanged across verification. Local/PR files, `extends`, scopes,
duplicate YAML keys and ambiguous or missing values do not establish this proof.

The deprecated queue-rule API's `allow_inplace_checks` field is not the runtime
verdict: [Mergify's API examples](https://docs.mergify.com/api/queues/) show `true` for both in-place and draft-PR
checks. Verify actual `mergeability_check.check_type = draft_pr` and unchanged
source heads in a disposable live rehearsal before cutover. GitHub blob stability
does not prove vendor ingestion; drain queues before configuration changes and
repeat this verification before resuming admission. Direct reuse of valid CI
without rewriting the source remains compatible.

The named queue **must** require `check-success = Loop-zero Eligibility` in
`queue_conditions`, with the trusted source and candidate publishers described
below deployed first. Preserve that condition on candidates (omit
`merge_conditions` to inherit it, or include the same check explicitly). For example:

```yaml
queue_rules:
  - name: main
    queue_conditions:
      - base = main
      - -draft
      - check-success = Loop-zero Eligibility
```

This is a required server-side gate: the command API has no expected-SHA field,
and a request can be processed after the source changes. The CLI's head-change
error refuses further delivery and cleanup; it cannot revoke an asynchronous
command atomically. A newly pushed head must lack eligibility until its own
review passes. Never deploy the adapter with only product-CI queue conditions.
Before pushing to a requested or queued PR, withdraw it, confirm removal, then
push, review the new head and explicitly admit it again. Do not interpret a CLI
failure as proof that Mergify has removed an outstanding request.

Provide an admin-scope Mergify application key in `MERGIFY_API_KEY`, or store it
in `~/.config/mergify/api-key` with mode `0600` in a private directory. The
environment variable wins. An invalid supplied credential fails the operation;
loop-zero does not fall back to another account. The key is sent only to the
fixed hosted Mergify API origin. GitHub Actions' usual `ghs_*` token is not a
supported Mergify API credential.

## Hosted review eligibility

For local GitHub reads, PR body updates, and review comments, an optional executable
`~/.config/loopzero/app-token` may print a fresh GitHub App installation token.
Loop-zero caches it for 50 minutes and uses the operator's `gh` login when the
helper is unavailable. Other GitHub writes and git push use the operator identity. Install the app on
every repository loop-zero delivers: reads through the app do not fall back on 404.

Set `[delivery].review_publishers` to the GitHub logins that publish model reviews.
Local readiness reads that list from the base configuration (legacy installations
without it use the local GitHub login). Hosted evaluation requires an explicit list;
its CI token's login is never a reviewer identity.

From a trusted base checkout, run:

```sh
python -m loopzero.eligibility --config workflow.toml --pr 123 --head <full-source-sha> --publish
```

Give that process pull-request read and commit-status write access.
It posts `Loop-zero Eligibility` as pending before reading live reviews/threads,
then success or failure on the specified source SHA. An API failure leaves pending.
The hosted Actions entry point succeeds with a pending status when its sole blocker
is missing trusted current-head review. The manual command above retains a failure
status and nonzero exit for that blocker; both paths keep merging blocked.
Do not check out or execute candidate code with this credential. Serialize all
refreshes for a PR, including manual refreshes; use trusted base configuration, never
PR configuration. A source push needs a status on its new SHA; base-only movement
does not invalidate an unchanged source review. This status does not replace
integration CI or a local `loopzero check` receipt.

For the supplied Actions publisher, create an environment named
`mergify-metadata`, restrict its deployment branches to the protected default
branch, and store `MERGIFY_API_KEY` as an environment secret rather than a
repository secret. Use the narrowest Mergify credential that can read queue status.
The environment prevents a workflow dispatched from another ref from receiving the
organization credential; it does not make a GitHub commit-status context unforgeable.

GitHub and Mergify match `Loop-zero Eligibility` by context name. A repository
writer who can add an Actions workflow can publish the same context with its
`GITHUB_TOKEN`. Before cutover, either explicitly accept that cooperative writer
boundary or replace this publisher with a dedicated GitHub App and bind the
required status check to that App's integration ID. Do not describe the Actions
publisher as protection from a malicious repository writer.

The evaluator preserves COMMENT model reviews with resolved blocking findings,
and the existing outdated-thread policy: outdated findings on other SHAs do not
block, but unresolved current critical/important findings do. Suggestions do not
block. Dismissing the latest review cannot restore an earlier approval.

Before queue adoption, wire source pushes, review submissions/edits/dismissal,
review-comment creation/edits/deletion and an explicit refresh to this publisher.
Actions has no review-thread resolution/reopening trigger. Until an App webhook
receiver exists, queue users must withdraw an item before altering findings or
resolving/reopening threads, refresh eligibility, then explicitly request delivery
again. External UI edits without that protocol are not covered. This is the
existing cooperative trust policy, not instantaneous invalidation: metadata can
change after the last read and before landing. Do not claim atomic review/merge
validation or enable unattended landing without accepting this boundary.

After cancellation or ejection, repair the cause and explicitly requeue with
`@mergifyio queue <name>` (the configured queue) as the whole PR comment, then
resume `loopzero merge --wait`.
The CLI never retries a failed admission automatically. Resume request markers
are scoped to the publishing GitHub login; use the same account across sessions.
