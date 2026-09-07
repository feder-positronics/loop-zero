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

## Connect the existing runtime entry points

Add one local instruction to the existing `AGENTS.md` directing Codex to read
`vendor/loop-zero/adapters/codex.md` for work using this workflow, and one to the
existing `CLAUDE.md` (or its existing instruction entry point) pointing to
`vendor/loop-zero/adapters/claude.md`. Record the source revision in these
project instructions, or explicitly direct the agent to `[core].revision` in
`workflow.toml`. Preserve existing contents, imports and skill directories.
These are explicit file pointers; no skill auto-discovery claim or installation
is needed. A runtime with no repository-instruction loading must be given the
adapter path explicitly. Record the actual runtime/version and loading evidence
when exercising each adapter in a real delivery.

Read the local contract and execute its reviewed setup command directly. The
core never executes TOML values. Idempotence of the repository's setup command
is the consumer's responsibility; rerun or inspect it before reporting success.

## Verify the pin

Python 3.11+ and Git are needed only for this optional read-only integrity check:

```sh
python3 vendor/loop-zero/tools/status.py --consumer . --source "$LOOP_ZERO_SOURCE"
```

Status checks every file's bytes, detects extra/missing files and rejects
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

## Upgrade consumers to 0.2.0

After the 0.2.0 PR is reviewed and merged, select the resulting full merge commit
SHA containing `core/VERSION` = `0.2.0`. In each consumer, replace the tracked
snapshot as above and update `[core].revision` and any explicit adapter revision
pointers in the same consumer PR. Do not use `0.2.0` as the pin or pin the
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
variable name containing `lease` or `nonce` (case insensitive), even if empty.
Repository-specific aliases and other commit credentials must also be excluded
by the runtime's allowlist. This check cannot prove absence of arbitrary aliases
or filesystem write authority. Configure ref/index isolation for hooks and all
validation descendants in the consumer's existing runtime; a linked worktree
shares refs and is not an isolation boundary. The pin check passes only a small
OS environment allowlist to its read-only Git children, with global/system Git
configuration disabled; it never launches consumer commands.

Selected status checks print exactly `pass` or `fail` on stdout, with failure
diagnostics on stderr and a nonzero failure exit. The 0.1.0 `VERIFIED` success
message is replaced; update any consumer parsing it. No source checkout is
required for the environment or known-gaps checks. Publication/review evidence
belongs only in the current-head PR body, using `core/HANDOFF.md`.
