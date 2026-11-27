# Setup

## Prerequisites

- `git` 2.40+, Python 3.14, [uv](https://docs.astral.sh/uv/).
- `gh` from GitHub's [package repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md)
  (tested with 2.101.0), logged in with `repo` scope, so system updates keep it
  current. Distribution packages lag (Ubuntu ships 2.46.0). Do not use the snap: its confinement cannot reach the keyring. Without root,
  unpack the [release](https://github.com/cli/cli/releases) tarball and put
  `bin/gh` in `~/.local/bin` ahead of `/usr/bin`; that copy does not update.
- Keep `gh`, `claude` and `codex` current. Tools that shape the build (`uv`,
  Python, a consumer's Node and package manager) change only with their pin in
  the repository, for example `uv` in `.github/workflows/checks.yml`.
- Linux with `bwrap` on `PATH` for default `loopzero check` and fresh local
  `loopzero review` runs. Bubblewrap uses Linux kernel namespaces.
- `claude` or `codex` CLI logged in; both if the reviewer must differ from
  the author.
- On CI or any unattended host give Claude a non-rotating API key or setup
  token; interactive OAuth rotates its token and is revoked on the second run.

## Install

Pin to a full commit SHA from `main` and upgrade by re-running with a new SHA.
The `v0.4.x` tags refer to the retired tool.
`loopzero --version` prints the revision of a Git-URL install; the verified
install below prints `unknown revision`, so record its checked-out SHA. Never
edit the installed package: a hotfix is a branch, a PR, and a reinstall from
the merged SHA.
User-wide:

```sh
uv tool install "git+https://github.com/feder-positronics/loop-zero@<sha>"
```

As a dev dependency when your project requires Python 3.14 or newer:

```sh
uv add --dev "loopzero @ git+https://github.com/feder-positronics/loop-zero@<sha>"
```

For a verified CI install, check out the full commit SHA and run these commands
from its repository root. The build constraints verify Hatchling and its build dependencies.
The lock export supplies the hashes for the runtime dependencies.

```sh
install_dir=$(mktemp -d)
uv build --python 3.14 --build-constraints build-constraints.txt --require-hashes --wheel --out-dir "$install_dir"
uv export --locked --no-dev --no-emit-project -o "$install_dir/runtime.txt"
uv venv --python 3.14 "$install_dir/venv"
python="$install_dir/venv/bin/python"
uv pip install --python "$python" --require-hashes -r "$install_dir/runtime.txt"
uv pip install --python "$python" --no-deps "$install_dir"/*.whl
"$install_dir/venv/bin/loopzero" --help
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
when you upgrade the CLI. Reconcile the skill links for added or removed
directories and keep the previous revision for rollback. Never edit the linked checkout. Both harnesses follow a
symlinked skill directory and resolve its relative links from the real files, so
the contract and references stay reachable. Do not also keep copies of these
skills in a repository: both would load.

If you roll back across [#732](https://github.com/feder-positronics/loop-zero/pull/732)
after GitHub may have accepted a Claude review, run `loopzero review --repost`
with the publishing CLI revision or a newer compatible revision. The rollback
CLI predating #732 expects a transcript where the newer publication has a
retention notice. Keep the original local receipt and confirm the existing
publication before resuming the rollback.

If the checkout is already at `~/.local/share/loopzero/source`, move it to a
separate path and use that path as `CHECKOUT`. If a skill destination is a real
directory, move it aside before linking that skill.

```sh
CHECKOUT=/path/to/loop-zero-checkout  # pinned to the CLI revision
SRC=~/.local/share/loopzero/source    # stable path; repoint it to upgrade
mkdir -p ~/.local/share/loopzero ~/.agents/skills ~/.claude/skills
python3 - "$CHECKOUT" "$SRC" <<'PYTHON' &&
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

checkout, source = map(Path, sys.argv[1:])
with TemporaryDirectory(dir=source.parent) as staging:
    link = Path(staging) / source.name
    link.symlink_to(checkout.resolve(strict=True), target_is_directory=True)
    os.replace(link, source)
PYTHON
(
  for d in "$SRC"/core/skills/*/; do
    n=$(basename "$d")
    ln -sfnT "$SRC/core/skills/$n" ~/.agents/skills/"$n" || exit
    ln -sfnT "$SRC/core/skills/$n" ~/.claude/skills/"$n" || exit
  done
)
```

Repositories customise through documentation only: `AGENTS.md`, including
folder-level `AGENTS.md` files, and the docs it links ("when X, read Y"). Project
docs add project detail; they never relax the contract. Point your agent's
repository's `AGENTS.md` at the shared [skill guide](https://github.com/feder-positronics/loop-zero/blob/main/core/skills/README.md)
and [credential guidance](https://github.com/feder-positronics/loop-zero/blob/main/core/CREDENTIALS.md).
These links follow `main`. For guidance that matches a pinned CLI, open the
same GitHub pages at the installed commit SHA. Codex and Claude Code (v2.1.277
or later) both read `AGENTS.md` directly. Do not add a `CLAUDE.md`, `.claude/CLAUDE.md` or
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

`loopzero start` fetches the base branch from a Git remote named `origin`.
Point it at the GitHub repository you set in `[repo] name`. Use
`git remote add origin <url>` to add it or `git remote rename <name> origin`
if you already configured it under a different name.

Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` in the
repository root, set `[repo] name`, and commit your initial `[checks]` commands
to the base branch before starting your first task. If the base has a
`workflow.toml`, `loopzero check` uses its checks configuration even when
`[checks]` is absent. To try task-side changes explicitly, run
`loopzero --config workflow.toml check` from the task worktree. Then:

- Before pushing the base, set `checks.commands` to checks your project can run.
  For a shell-only first check, use `commands = ["test -f greeting.txt"]` under
  `[checks]` and commit `greeting.txt` in your first task. Replace or omit the
  example's uv-specific `scratch`, `env`, `writable`, and `ro_paths` entries
  to match your tools. With just shell checks, you can omit all four.
- Configure hosted check names, mounts, scratch and environment in the
  [commented workflow example](workflow.example.toml). See the contract's
  [sandbox rules](core/CONTRACT.md#sandbox-rules-for-checks) for runtime isolation.
- Host paths in the trusted workflow must work on every delivery machine. Use a
  machine-specific workflow with `--config` on check, ready and merge when paths differ.
- `[delivery] reviewer_ro_paths` — optional absolute reviewer CLI/runtime paths;
  the [example](workflow.example.toml) states the default and protected-state limits.
- Check, pr, review, ready, merge, resolve, and status load the workflow from the
  remote-tracking delivery branch selected by `[repo] base` in the workflow
  at `origin/HEAD`. Missing trusted policy blocks these commands. Fetch and
  run `git remote set-head origin --auto` if the local default-branch pointer
  is missing. Upgrade delivery pins before adding workflow keys they need to
  recognize. Supply `--config <path>` during bootstrap or to override policy.
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
  python3 - "$HOME/.config/typesafe" "$HOME/.config/typesafe/api-key" <<'PYTHON'
import os
import stat
import sys

for path in sys.argv[1:]:
    print(f"{stat.S_IMODE(os.stat(path).st_mode):o} {path}")
PYTHON
```

This should show `700` for the directory and `600` for the file.
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
`claude`, `codex` and `loopzero` commands. Install the monitor and its three shims
from the [pinned source checkout](#connect-your-agent):

```sh
ACCOUNTS=~/.local/share/loopzero/source/scripts/ai-accounts
install -m 755 "$ACCOUNTS/ai-accounts" ~/.local/bin/
mkdir -p ~/.local/lib/ai-accounts/bin
for n in claude codex loopzero; do ln -sf ~/.local/bin/ai-accounts ~/.local/lib/ai-accounts/bin/$n; done
```

Put the shims first on PATH by making this the last line of `~/.profile` and
`~/.bashrc`, after anything that prepends `~/.local/bin`:
`export PATH="$HOME/.local/lib/ai-accounts/bin:$PATH"`. Point an agent host's binary
path at `~/.local/lib/ai-accounts/bin/claude` (and `codex`). Copy
`"$ACCOUNTS/ai-accounts.service"` and `"$ACCOUNTS/ai-accounts.timer"` to
`~/.config/systemd/user/`, then run
`systemctl --user enable --now ai-accounts.timer`.

To review with a pinned CLI and managed credentials, set `PINNED_BIN` to the
directory containing the pinned `loopzero` executable:

```sh
PATH="$HOME/.local/lib/ai-accounts/bin:$PINNED_BIN:$PATH" loopzero review
```

- Claude accounts are long-lived tokens: run `claude setup-token` per account and
  pipe each into `ai-accounts add claude <name>`. Codex accounts are separate
  `CODEX_HOME`s sharing `~/.codex` except `auth.json`: `ai-accounts add codex <name>`,
  then log in as it prints; the monitor refreshes standby Codex logins before they expire.
- The monitor checks accounts every five minutes. Claude prefers Fable and Opus,
  then Opus only. Codex keeps the current usable account until it is spent at 95%
  of any limit by default, then prefers an account with every model available.
  Claude reads the OAuth usage API at most every 30 minutes and uses one-token
  header probes between reads. `ai-accounts status` shows accounts, tiers and
  per-window utilization and resets. `weekly_used_percent` is the overall 7d
  usage, null when unknown.
- Only `exec` (including `--identity`) refuses non-owner Claude launches with exit 3
  at 85% utilization in any reported window, including the Fable weekly
  window, or when that account's cached
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
  The loopzero shim prefers executable Codex in `~/.local/lib/codex-reviewer`
  when present; keep `codex` and its matching `codex-code-mode-host` side by side
  there with executable permissions so sandboxed reviews can launch the helper.
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

Commit and push `workflow.toml` to the configured remote base
(`origin/main` by default) before your first task. `loopzero start` fetches
that base and creates the task worktree from it.

```sh
loopzero start hello-loopzero        # new worktree + branch; cd to the printed path
$EDITOR .loopzero/task.md             # fill Goal, Problem, Context and Acceptance
```

Make and commit a change in the task worktree, then run `loopzero check` there;
for a long check suite, run `loopzero pr` first so the draft exists
([step 2](core/CONTRACT.md#the-six-steps)).
Afterward, `loopzero status` shows the next step and requires authenticated
access to the configured GitHub repository even before a PR exists. See the
[workflow diagram](docs/WORKFLOW.md#1-from-idea-to-merged) and
[Blockers](core/CONTRACT.md#blockers) for the rest of the workflow.

### Check receipts with remote execution

Use one pinned CLI revision and the same effective settings for `check`,
`ready`, and `merge`. If a shared install can change mid-task, use the
[isolated install](#install). The coordinator runs in the task worktree and
writes the receipt.

Run the whole check locally, through the host's slot wrapper where host policy
requires one. If you used the isolated install, run these from the task
worktree with the `install_dir` from that recipe.
Make a scratch clone standalone before queueing, for example with
`git clone --no-local`. If a Git object alternate is outside the check sandbox,
`loopzero check` refuses it before any stage runs. Move the task branch and
`.loopzero/task.md` to the standalone clone, then recheck the same commit.

```sh
export PATH="$install_dir/venv/bin:$PATH"
loopzero pr      # open the draft before a long check
loopzero check
# After reviewing the PR, keep this PATH for delivery.
loopzero ready --wait
loopzero merge --wait
```

A whole-job remote worker writes its own receipt. Ready can use it only if it
passes the existing checks for a clean, passing receipt at the exact head,
with the current ordered commands and matching settings fingerprint. An older
worker can omit the fingerprint. Explicit check settings must match. GitHub required
CI supplies separate evidence. The [owner queue route](core/CONTRACT.md#blockers)
applies only under its documented conditions.

## Removing an abandoned task worktree

For a merged PR, retry cleanup from a durable directory with
`(cd "/absolute/path/to/task-worktree" && loopzero merge)`.
For a closed, unmerged PR outside the host retention job's scope, the task owner
can remove the worktree manually after confirming it is abandoned and no session
or job still uses it. In GitHub, verify the closed PR belongs to this repository
and task branch, and copy its full head SHA. Preserve any delivery evidence
required by the contract and inspect ignored files for local work or evidence
before removing the tree. Resolve any shared stash or uncertain ownership first.

Save this script outside the task worktree. From a durable directory, run
`sh /path/to/script /absolute/path/to/task-worktree <task-slug> <pr-head-sha>`.

```sh
(
    set -eu
    tree=$1
    branch="refs/heads/lz/$2"
    pr_head=$3
    git_dir=$(git -C "$tree" rev-parse --path-format=absolute --git-common-dir)
    test "$(git -C "$tree" symbolic-ref --quiet HEAD)" = "$branch"
    test "$(git -C "$tree" rev-parse HEAD)" = "$pr_head"
    published=$(git -C "$tree" ls-remote --exit-code origin "$branch")
    test "$published" = "$(printf '%s\t%s' "$pr_head" "$branch")"
    stash=$(git --git-dir="$git_dir" stash list)
    test -z "$stash"
    changes=$(git -C "$tree" status --porcelain --untracked-files=all --ignore-submodules=none)
    test -z "$changes"
    git --git-dir="$git_dir" worktree remove -- "$tree"
)
```

If any check fails, leave the worktree in place. The command keeps both the
local and published branches.

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
Mergify queue-rule name. `loopzero merge` posts the unnamed `@mergifyio queue`
command (Mergify deprecates naming a queue and picks the first whose conditions
match) once for the current source head, then confirms membership in the
configured queue through `https://api.mergify.com/v1`. A command comment is only a
request; a behind branch is treated as queued only after that API confirms it. A
repository queue rule may admit the PR instead; Mergify's "already queued" reply
to the command is then harmless.

Set `merge_queue.mode: serial`. For draft-PR integration checks, set
`merge_queue.max_parallel_checks` greater
than one, or to one only when the matching queue rule sets an explicit integer
`batch_size` greater than one. In-place updates (one parallel check and batch size
one) rewrite the reviewed source head and are unsupported. Admission checks the
live serial queue's identity and reads `.mergify.yml` from GitHub's default
branch, requiring that explicit integer parallelism and batch size and the
matching queue name. The configuration blob
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
push, review the new head and admit it again. Do not interpret a CLI
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

Advance the consumer CI pin that supplies `loopzero.hosted` or
`loopzero.eligibility` (the loop-zero checkout ref or installed package revision)
before relying on a new reader or writer behaviour. To refresh an open PR judged
by an older reader, dispatch the eligibility workflow for it, or run this from a
trusted base checkout with the trusted Loop-zero pin and the source PR head SHA:

```sh
uv run --no-project --with "git+https://github.com/feder-positronics/loop-zero@<sha>" \
  python -m loopzero.eligibility --config workflow.toml --pr 123 --head "<full-source-sha>" --publish
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

Use the [Actions publisher workflow](.github/workflows/eligibility.yml) as a
reference. In a consumer, install the pinned loop-zero package for both jobs
and remove the repository-specific `PYTHONPATH: src`. Run from the consumer
checkout at its trusted default branch. Create an environment named
`mergify-metadata`, restrict its deployment branches to the protected default
branch, and store `MERGIFY_API_KEY` as an environment secret rather than a
repository secret. Use the narrowest Mergify credential that can read queue status.
Only the candidate job uses this environment and credential. The source job runs
without either. Restricting the environment to the protected default branch limits
credential access, while commit-status publishers still share a context namespace.

Require `Loop-zero Eligibility` in GitHub branch protection and in
`[checks].required_ci` on the trusted base. GitHub and Mergify match it by
context name. A repository
writer who can add an Actions workflow can publish the same context with its
`GITHUB_TOKEN`. Before cutover, either explicitly accept that cooperative writer
boundary or replace this publisher with a dedicated GitHub App and bind the
required status check to that App's integration ID. Do not describe the Actions
publisher as protection from a malicious repository writer.

The evaluator preserves COMMENT model reviews with resolved blocking findings.
See [Blockers](core/CONTRACT.md#blockers) for the outdated-thread rule. Suggestions
do not block. Dismissing the latest review cannot restore an earlier approval.

Before queue adoption, wire source pushes, review submissions/edits/dismissal,
review-comment edits/deletions and an explicit refresh to this publisher.
Actions has no review-thread resolution/reopening trigger. Until an App webhook
receiver exists, queue users must withdraw an item before altering findings or
resolving/reopening threads, refresh eligibility, then explicitly request delivery
again. External UI edits without that protocol are not covered. This is the
existing cooperative trust policy, not instantaneous invalidation: metadata can
change after the last read and before landing. Do not claim atomic review/merge
validation or enable unattended landing without accepting this boundary.

### Requeue after cancellation or ejection

After cancellation or ejection, repair the cause and requeue: an unchanged head
needs `@mergifyio queue` as the whole PR comment; `loopzero merge` requests a new
head, or a repository queue rule readmits it. Then resume `loopzero merge --wait`.
The CLI never retries a failed admission automatically. Resume request markers
are scoped to the publishing GitHub login; use the same account across sessions.
