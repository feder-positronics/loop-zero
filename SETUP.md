# Setup

## Prerequisites

- `git` 2.40+, Python 3.14, [uv](https://docs.astral.sh/uv/).
- `gh` logged in with `repo` scope; `bwrap` on `PATH` (`loopzero check`
  refuses to run without it).
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

Point your agent's repository instructions at that checkout's
`core/skills/README.md` and `core/CREDENTIALS.md`. Use the relevant skill from
the catalog for each task. Keep the source directory structure intact so
relative links resolve; copying an individual `SKILL.md` loses its references.
For TypeSafe work, explicitly request `typesafe-ai` from that catalog. In this
repository, `.agents/skills` links `plan`, `implement`, `work-issue`, and
`typesafe-ai` to their canonical skills for discovery. Other consumers must
connect their own agent.

For model routing, copy [models.toml](models.toml) into the consuming repository
and adjust its model IDs and effort values to the available runtimes. Point
repository instructions at the shared [delegation guidance](docs/DELEGATION.md).
The agent reads the category/profile mapping directly, checks availability,
and passes the selected model and effort to its delegation tool. There is no
additional CLI, scheduler, or model catalog to upgrade. Formal `loopzero review`
continues to use reviewer families from `workflow.toml`; `models.toml` does not
change it.

Configure application API keys using the shared convention below. This requires
no changes to `workflow.toml` and no Bitwarden CLI installation if you paste
the key from the Bitwarden app at the hidden setup prompt.

## Configure

Copy [workflow.example.toml](workflow.example.toml) to `workflow.toml` in the
repository root and set `[repo] name`. Then:

- `[checks] commands` — must exit zero before a PR opens; for uv projects
  `uv run --group dev ruff check .` and `uv run --group dev pytest -q` work.
- `[checks] required_ci` — exact GitHub check names green before merge.
- `[checks] ro_paths` — `/home` is hidden; list toolchain paths under it in
  full (`/home/<you>/.local/bin`, `/home/<you>/.local/share/uv`).
- `[checks] writable` — the worktree is read-only except per-run scratch over
  `[checks] scratch` (default `.venv`, `.ruff_cache`, `.pytest_cache`, created
  as empty git-invisible dirs). List a shared cache like `~/.cache/uv` here; a
  check can then write to it (trust). uv projects must commit `uv.lock`.
- `[checks] env` — e.g. `{ UV_CACHE_DIR = "/home/<you>/.cache/uv" }`; wins over
  sandbox defaults and host variables. `PATH` and `HOME` cannot be overridden.
- `[delivery] reviewer_ro_paths` — optional absolute reviewer CLI/runtime
  paths. The default is the selected binary's resolved directory; Claude may
  need its npm or bun prefix listed.
- The first `check` in a fresh worktree needs `network = true` or a warm uv
  cache in `writable`: warm it once by running your check commands on the
  host (for uv, `uv sync --group dev`), which also fetches build backends. `PYTHONDONTWRITEBYTECODE`, `RUFF_CACHE_DIR`,
  `UV_CACHE_DIR`, `PYTEST_ADDOPTS` are preset in the sandbox.

## API keys for local agents

All API-key integrations follow the [credential convention](core/CREDENTIALS.md):
use the integration's documented environment variable first, then
`~/.config/<service>/api-key`. This is the default for local agent work; existing
provider CLI logins and reviewer authentication retain their own configuration.
Document each integration's variable, service directory, endpoint, and intended
account/environment. Secret values never belong in `workflow.toml`.

The file is a convenient local copy, shared across worktrees by agents running
as your user. It is plaintext and readable by processes with that user's access.
Keep the primary copy in Bitwarden. For stronger isolation, inject a key from
Bitwarden into only the calling process instead of creating the file; do not
pass an unlocked vault session to the agent. CI and production should use their
platform's secret store or workload identity, with separate credentials.

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

A host agent following the convention can read the file on its next API call;
no application restart is needed to create it. An application that caches its
key may need restarting after rotation. Environment variables are inherited at
process launch; exporting one elsewhere cannot update an existing agent.
Installing a skill does not change an SDK's lookup behavior: agents resolve the
key in their calling process, and applications need an explicit loader if they
want the same fallback.

Rerun the setup to replace a rotated key. Clear stale environment overrides,
which take precedence over the file. Remove the file and unset the documented
variable to remove local sources; revoke at the provider to invalidate copies
already loaded by processes. Do not paste keys into chat or print them to verify
configuration. Report only whether a usable credential source is available.

Keep live evaluations separate from required checks. Application keys and their
directories must not be added to check or review sandbox allowlists or mounts.
The sandboxes' existing tool authentication is unchanged.

### TypeSafe credentials (optional)

The [TypeSafe skill](core/skills/typesafe-ai/SKILL.md) uses:

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
Lock the vault and remove `BW_SESSION` before starting the agent. An existing
nonempty but invalid `TYPESAFE_API_KEY` causes an authentication failure; fix or
unset it rather than silently switching to the file.

## First run

```sh
loopzero start hello-loopzero        # new worktree + branch; cd to the printed path
$EDITOR .loopzero/task.md             # fill Context, Problem, Goal and Acceptance
# ...make a small change and add a test...
loopzero check                        # exit 0, or FAIL; renders the PR Validation section when open
loopzero pr                           # draft PR opens; URL printed
loopzero review                       # model review posted on the PR
loopzero check && loopzero review     # after fixing blocking threads: delta review
loopzero ready                        # marks PR ready if head/findings/CI pass
loopzero merge                        # merges, deletes branch and worktree
loopzero status                       # at any point: next step and why
```

## Merging under a merge queue

When the base branch uses a GitHub merge queue, `loopzero merge` enqueues the
PR and exits after printing that it is queued. Nothing polls. Run
`loopzero merge` again once the queue has landed it; that run verifies the
merge commit, deletes the remote branch and removes the worktree. The
repository must allow auto-merge (Settings, General, "Allow auto-merge"),
otherwise `gh pr merge` fails with "Auto merge is not allowed". Set
`delivery.merge = "queue"` so the queue owns the merge method.
