# Setup, status and updates

## Deposit the snapshot

Use an inspected local checkout of `https://github.com/feder-positronics/loop-zero`
and select its full 40-character commit SHA. A branch or tag is not a pin.
The source `core/` directory is the entire portable distribution; tests and
source documentation remain here. The example consumer location is
`vendor/loop-zero/` relative to the consumer repository root.

For a new consumer, from its root, with `LOOP_ZERO_SOURCE` set to the source
checkout and `LOOP_ZERO_REVISION` set to the inspected full SHA:

```sh
mkdir -p vendor/loop-zero
(
  set -e
  LOOP_ZERO_TEMP=$(mktemp -d)
  trap 'rm -rf -- "$LOOP_ZERO_TEMP"' EXIT
  git -C "$LOOP_ZERO_SOURCE" archive --format=tar --output="$LOOP_ZERO_TEMP/core.tar" "$LOOP_ZERO_REVISION" core/
  tar -xf "$LOOP_ZERO_TEMP/core.tar" --strip-components=1 -C vendor/loop-zero
)
```

Inspect the selected source before extraction; stop if archive or extraction
fails. Never overlay an existing snapshot: for an
update, preserve local work, remove only the previously tracked snapshot in the
owned worktree, then deposit the replacement and review the diff. These are
ordinary Git file operations, not an updater. Repeat setup by verifying the
existing pin; an unchanged snapshot requires no copying or adapter edits.

Create a repository-owned `workflow.toml`:

```toml
profiles = ["python"]

[core]
repository = "https://github.com/feder-positronics/loop-zero"
revision = "REPLACE_WITH_INSPECTED_FULL_COMMIT_SHA"
path = "vendor/loop-zero"

[package]
env_prefix = "MY_PRODUCT"
audit_root = ".audit"
skills_dir = ".cursor/skills"
skill_mirrors = [".agents/skills", ".agent/skills", ".claude/skills"]
product_name = "My Product"

[toolchain]
backend_dir = "src"
frontend_dir = "web"
scripts_dir = "scripts"

[toolchain.commands]
worktree_setup = "make setup"
docs_verify = "make docs-verify"
test_backend_unit = "make test-unit"

[checks]
required = ["lint", "types", "tests", "changed-links"]
advisory = ["optional-tests"]
scheduled = ["external-links", "dependency-audit", "cost-usage", "docs-governance"]

[paths]
source = "src"
tests = "tests"
constraints = "AGENTS.md"
setup_reference = "docs/development.md"

[commands]
setup = ["cd . && make setup"]
check_fast = ["cd . && make test-unit"]
check_integration = ["cd . && make test-integration"]
```

Replace example paths and commands with the repository's existing ones.
Commands may be a string or an ordered array of strings, with working directory
explicit (including a root-working-directory statement in local instructions).
The task selects and records actual narrowed checks; this list does not mandate
running unrelated suites. Integration prerequisites belong in the local setup
reference. When there is no integration surface, use an explicit string such
as `not-applicable: documentation-only repository, no integration behavior`.
Missing applicable setup/check capability is a blocker, not not-applicable.
Profiles are selected from `python`, `fastapi`, `nextjs`, `angular`; profile
selection does not import commands. Keep local secrets outside this file.
The full set of supported skill substitution keys and compatibility defaults is
documented in [`core/skills/README.md`](core/skills/README.md). Declare the
commands a selected governance skill names; defaults exist to preserve the
initial IntelFlo cutover byte for byte, not as a promise that another consumer
has those targets.

## Install the package

The executable mechanisms live in the `loopzero` package. Install it from the
same full commit SHA as the snapshot, as a Git dependency in the consumer's
existing Python project, with only the extras the consumer's runtimes need:

```sh
uv add "loopzero[claude,codex] @ git+https://github.com/feder-positronics/loop-zero@$LOOP_ZERO_REVISION"
```

The base package has no third-party dependencies and requires Python 3.12 or
later on Linux. `loopzero status` reports the installed package version and
the vendored snapshot's `core/VERSION`; they must be equal. `loopzero sync`
renders the consumer's runtime wiring from `workflow.toml` into checked-in
files, and `loopzero sync --check` fails when those files drift. Sync operations
serialize on an exclusive `.loopzero/sync.lock`. A replacement sequence creates
`.loopzero/generated.json.partial`; if a process stops mid-sequence, the marker
makes the next `sync --check` report the interruption explicitly, and a
successful `sync` clears it. The lock and final re-read bound honest concurrent
syncs, but a human edit made during the final multi-file replace loop can still
race a replacement; stop editing generated targets while sync runs. Run the
check in CI.

The sync also materializes governance and vendored methodology skills in the
configured canonical directory, defaults to `.cursor/skills`, and maintains
relative symlink mirrors in `.agents/skills`, `.agent/skills`, and
`.claude/skills`. Consumer-owned skills remain untouched. Managed ownership is
recorded in `.loopzero/skills-manifest.json`; an unmanifested skill directory is
never overwritten. `loopzero sync --dry-run` reports the same drift without
writing and, unlike `--check`, does not use drift as a failing exit status.

## Connect the existing runtime entry points

Add one local instruction to the existing `AGENTS.md` directing Codex to read
`vendor/loop-zero/adapters/codex.md` for work using this workflow, and one to the
existing `CLAUDE.md` (or its existing instruction entry point) pointing to
`vendor/loop-zero/adapters/claude.md`. Record the source revision in these
project instructions, or explicitly direct the agent to `[core].revision` in
`workflow.toml`. Preserve existing contents, imports and skill directories.
The canonical skill directory and declared mirrors are generated by sync; local
product skills already present there remain consumer-owned. A runtime with no
repository-instruction loading or skill discovery must be given the adapter or
skill path explicitly. Record the actual runtime/version and loading evidence
when exercising each adapter in a real delivery.

Read the local contract and execute its reviewed setup command directly. This
package skeleton never executes TOML hook values. Hook execution, including the
validation-child environment allowlist and filesystem sandbox, belongs to the
kernel implementation. Idempotence of the repository's setup command is the
consumer's responsibility; rerun or inspect it before reporting success.

Before reviewing hook-aware policy, provide an explicit trusted base. `--base`
is the trusted form and accepts only a full commit SHA. `--base-ref` establishes
provenance only to a named local ref: it accepts full
`refs/heads/<branch>` or `refs/remotes/<remote>/<branch>` names, verifies that
the ref exists, resolves it, and prints the resulting SHA. It rejects short
names and revision expressions. Both modes reject replacement objects and
non-blob base policy files. Hook executables resolve only in `/usr/bin` and any
explicit `--path-entry` directories, never the caller's inherited `PATH`; every
component of an allowed directory and executable must be a non-symlink:

```sh
loopzero policy lint --base "$TRUSTED_BASE_SHA"
loopzero policy lint --base-ref refs/remotes/origin/main
```

Omitting the base is `UNVERIFIED` and exits nonzero. Use `--no-hooks` only when
the requested lint deliberately excludes hooks; that mode reports validity but
does not print a trust `pass`.

## Verify the pin

Python 3.11+ and Git are needed only for this optional read-only integrity check:

```sh
loopzero status --source "$LOOP_ZERO_SOURCE"
```

The installed command runs `core/tools/status.py` from the trusted source
checkout, never from the vendored candidate snapshot. Status checks every
file's bytes, detects extra/missing files and rejects
symlinks. It requires the pinned commit in the local source checkout and exits
nonzero when proof is unavailable or mismatched. It does not fetch, execute
commands from TOML, validate the rest of the local contract or product setup, certify remote origin
ownership, or
prove runtime activation. Establish the trusted source through normal review;
`repository` is provenance metadata, not authentication. No source checkout is
needed to use the vendored instructions after verification.

`core/VERSION` labels the contract version; the commit SHA remains authoritative.
Update versions when changing the shared contract, pin the resulting reviewed
commit and keep the snapshot identical across pilot consumers. If one consumer
exposes a shared defect, fix it here once and revalidate affected acceptance in
both consumers on the same revision. Never hide a local fork inside the snapshot.

## Upgrade consumers to 0.2.1

After the 0.2.1 PR is reviewed and merged, select the resulting full merge commit
SHA containing `core/VERSION` = `0.2.1`. In each consumer, replace the tracked
snapshot as above and update `[core].revision` and any explicit adapter revision
pointers in the same consumer PR. Do not use `0.2.1` as the pin or pin the
pre-merge candidate. Verify bytes and rerun consumer acceptance on that exact
SHA; use the same pin across pilot consumers. This source release does not
itself update consumers or establish product portability.

Create one consumer-owned `KNOWN-GAPS.md` outside the snapshot, using the format
in the contract, and add its deterministic cap check to the consumer's CI:

```sh
python3 vendor/loop-zero/tools/status.py --known-gaps KNOWN-GAPS.md
```

Inside an already isolated validation child, check the environment with
`python3 vendor/loop-zero/tools/status.py --check-child-env`. It rejects any
variable name containing `lease` or `nonce` (case insensitive), even if empty,
except `lease` immediately preceded by `re`. Ordinary `RELEASE_CHANNEL` and
`RELEASE_VERSION` metadata is allowed; `RELEASE_NONCE` or `RE_LEASE` is not.
Repository-specific aliases and other commit credentials must also be excluded
by the runtime's allowlist. This check cannot prove absence of arbitrary aliases
or filesystem write authority. Configure ref/index isolation for hooks and all
validation descendants in the consumer's existing runtime; a linked worktree
shares refs and is not an isolation boundary. The pin check passes only a small
OS environment allowlist to its read-only Git children, with global/system Git
configuration disabled; it never launches consumer commands.

Pin-verified status checks print exactly `pass` or `fail` on stdout after the
informational version line, with failure
diagnostics on stderr and a nonzero failure exit. The 0.1.0 `VERIFIED` success
message is replaced; update any consumer parsing it. No source checkout is
required for the environment or known-gaps checks. Publication/review evidence
belongs only in the current-head PR body, using `core/HANDOFF.md`.

Without `--source`, `loopzero status` reports version equality as information,
exits zero, and deliberately emits no `pass`; version strings alone do not
authenticate snapshot bytes.

## Check policy and execution evidence

Adopt the `[checks]` table above using exact consumer CI check names. All three
arrays are required, may be empty, and must be disjoint with no duplicates.
Required and advisory are diff-scoped deterministic checks; scheduled contains
repository-health checks. Configure scheduled jobs against `main` to maintain
one health issue, and remove health contexts from required branch protection.
Configure the CI's infrastructure retry once and weekly override review per
`core/CONTRACT.md`; these integrations belong to the consumer, not this package.
Create consumer-owned `CHECK-OVERRIDES.md` only when an override is actually used.

```sh
loopzero checks
loopzero checks --results check-results.json
```

The read-only command uses the TOML required-list route; it makes no network
requests and does not query or change branch protection. Maintain alignment via
normal consumer review. Optional results are a JSON object keyed by exact check
name, for example:

```json
{"tests": {"conclusion": "network_timeout", "attempts": 2}}
```

Conclusions are `success`, `failure`, `pending`, `runner_loss`,
`cancelled_concurrency`, or `network_timeout`. Attempts is a positive integer
(default 1): the cumulative number of executions of that check for the same
candidate head, including the original attempt, never reset by a manual or
scheduled rerun (a per-run attempt counter such as GitHub's `run_attempt` is
not sufficient on its own). Infrastructure reasons require positive
identification by the consumer; a generic cancellation is insufficient. Absent
results are pending. Output is JSON with name, group, class, conclusion,
`blocks`, and action. `blocks` is true for every required check without a
`success` conclusion, including pending and infrastructure results; consume
that field rather than matching action text. Exit 0 means the report was
produced, **not that checks passed**. Invalid/missing input exits 1 with
`UNVERIFIED` on stderr. Files are read as UTF-8. The command executes no TOML
commands, applies no overrides, retries nothing, and writes no files.
