# Kernel integration

The kernel is Linux-only and uses the standard library, Git, OpenSSL and
bubblewrap. Install `loopzero` in the interpreter that launches its Python
entry points. Invoke Python entry points with `python -m loopzero.kernel.NAME`;
file-path execution and sibling-module loading are no longer supported.

Initialize one consumer per coordinator process **before importing mechanisms**:

```python
from loopzero.config import load_profile
from loopzero.kernel.settings import KernelSettings, configure

profile = load_profile(consumer_root)
settings = KernelSettings.from_profile(profile)
configure(settings)

from loopzero.kernel import authority, ledger, sandbox
```

`KernelSettings` is immutable. `from_profile` takes the environment prefix,
audit/state roots and toolchain from `Profile`; the other fields can be supplied
with `dataclasses.replace` before `configure`. `child_environment()` serializes
these settings for installed-package subprocesses. The bootstrap variables are
`LOOPZERO_ENV_PREFIX`, `LOOPZERO_KERNEL_SETTINGS` and `LOOPZERO_AUDIT_ROOT`.
Without configuration, the namespace is `LOOPZERO`. Selecting `INTELFLO`
reproduces its environment names, state/temp prefixes, ledger hash domain,
legacy signature namespace, and historical public key byte-for-byte. Other
namespaces explicitly supply `legacy_public_key` when adopting legacy records.
Persisted legacy contract identifiers remain readable through `legacy_contract`.

## Validation and credentials

Use `sandbox.run_validation_child(argv, worktree=...)` for validation. It builds
read-only mounts over the worktree Git metadata and the linked worktree common
Git directory, filters the environment, and closes inherited descriptors. Every
caller-selected mount destination is walked without following symlinks and is
rejected if it aliases `/`, `/proc`, `/sys`, or `/dev`. That walk follows the
source-tree mapping of every earlier mount, so a symlink exposed by one mount
cannot redirect a later destination. Caller mounts also cannot be placed below
the builder's read-only Git or authority seals.
Namespace failure is a failed prerequisite, not an unsandboxed fallback.
`validation_command` builds the filesystem boundary for an existing process
adapter; that adapter must also apply the environment and descriptor boundary.
The lower-level `command` function retains its original explicit mount controls
for privileged coordinators; it is not itself the validation-child API.

`sandbox.codex_subscription_credential` requires an injected `credential_broker`.
The callable receives `requested_runtime_s` and returns a context manager yielding
one owner-private descriptor. The broker owns renewal and closure. The kernel
validates and snapshots its bytes, removes durable refresh authority, and lends
only a sealed/read-only descriptor. It does not import runner SDKs or read
provider credential stores.

## Review adapters and jobs

`seams.configure(**adapters)` registers the named A4 review operations used by
retained projections and archive validation. `ADAPTER_NAMES` lists the complete
surface. A missing adapter raises `MissingAdapter`; it never means accepted
review evidence. Composition-root registration remains TODO(A4).

`job.sh` uses `LOOPZERO_PYTHON` for the approved installed-package interpreter.
The interpreter must be a symlink-free executable outside the consumer
repository and every linked worktree.
Namespaced `JOB_DIR`, `JOB_TIMEOUT` and `DELIVERY_ROOT` retain their original
semantics. Its privileged dispatcher and continuation paths are supplied through
`settings.toolchain['dispatcher']`, `['host_dispatcher']` and
`['continuation_runner']`. The existing command-shape, descriptor and signed-result
checks still apply to those paths. Candidate protection receives the consumer
repository explicitly rather than deriving it from the package installation.
Bound ordinary jobs receive the filesystem sandbox described above: the host
view, the account home, and the job authority root are read-only, the job
directory itself is sealed (reconcile, clean, and status trust its
`reconciliation.json`, `pid`, and `pid-identity.json`), and the coordinator
authority directory and `~/.ssh` are hidden behind empty overlays so their
keys and ledgers are unreadable, not merely unwritable. Only the worktree and
a private `TMPDIR` are writable. A terminal artifact that the bound command
produces itself must therefore be named inside the worktree (for example
under the audit root); that is the one launcher-nameable writable location.
Bound jobs also lose environment-carried credentials. Unbound jobs are
detached command-runner children, not validation children; they are stripped
of only the worktree-lease family (`sandbox.strip_worktree_lease_environment`:
descriptor, boundary, owner pid, nonce) so they cannot re-enter the launcher's
lease, and they keep credentials such as `GITHUB_TOKEN` and `SSH_AUTH_SOCK`
so agents can still run `gh` and `git push` through them. Callers must use
`sandbox.run_validation_child` or `job.sh consumer-hook` when the validation
environment, descriptor, network, and Git boundaries are required.

Final-CI reproduction is no longer a job-runner special case. Invoke
`job.sh consumer-hook` with explicit `--base`, `--head`, `--task-id`,
`--result-artifact`, `--coordinator-public-key`, and `--hook` arguments. Base and
head are full commit SHAs; neither is read from serialized settings. Before
launch, the kernel proves that `HEAD` is the approved commit and hashes the
complete exposed worktree independently of its mutable index. The filesystem
digest records regular-file link counts and deterministic in-tree inode classes;
a link whose other names are outside the worktree is rejected. An intentional
dirty-candidate run must additionally pass `--allow-dirty-tree`; it is identified
with `git write-tree` through an isolated Git directory and index. Ignored files
are forced into that tree, while the root `.git` administrative entry is sealed
read-only by the child sandbox. Candidate local Git config is screened before
repository-directed Git runs, then replaced in the child with only fixed
`core.repositoryformatversion=0` and `core.bare=false`; no `remote.*` or
`branch.*` configuration is retained. The isolated repository has no candidate
config and a highest-precedence attributes file disables filters, working-tree
encoding, ident and EOL conversion. Candidate `.gitattributes` content is itself
hashed but cannot change how any bytes are hashed; nested Git worktrees are
rejected because their checked-out contents cannot be represented by the outer
tree. Every Git process also disables global/system config, replacement objects,
prompts and optional locks and supplies explicit hooks, fsmonitor, worktree,
symlink and attribute overrides. The kernel resolves the base, reads privileged
hooks with `config.effective_hooks`, and launches them with
`sandbox.run_validation_child` before acquiring job authority.

Executable selection uses the package-wide symlink-free allowlist. This trusts
only `argv[0]`. Hook operands, interpreted scripts, and candidate test inputs are
candidate-controlled by design. Hooks must be hermetic: they may write only to
the private temporary and cache paths supplied in their environment, never to
the exposed worktree. Python bytecode and pytest, uv, npm, pnpm, and XDG caches
are redirected accordingly. A zero child exit is therefore not acceptance. The
parent's evidence is the actual child exit code plus equality of the complete
source identity before and after execution; only then does it verify a
coordinator-authorized result artifact bound to the task ID, base SHA, approved
head SHA, exact clean commit or dirty tree SHA, hook name, and exact resolved
command vectors. The artifact is pre-authorization for those fields, not an
attestation that the child ran. Result files are
opened nonblocking and without symlink following, must be bounded regular files,
and are read under a deadline. Unsigned/malformed/unreadable, tampered, and
misbound artifacts are distinct failures. Candidate code is never imported or
executed by that verifier.

The toolchain also accepts `interpreter` (sandbox runtime mounting) and
`formatter_modules` (the approved formatter installation). Missing formatter
configuration produces no format-equivalence proof. IntelFlo interpreter
discovery retains `fastapi_backend/.venv/bin/python`; other consumers configure
`interpreter` explicitly.

## Shipped hooks

Shell hooks obtain namespaced values through `settings.sh`. CI mirror helper and
lane executables are named `*_HOOK` injection points. Their complete list can be
read with `rg 'loopzero_consumer' ../hooks/ci-mirror-check.sh`. Missing required
executables fail. Backend/frontend roots use `LOOPZERO_BACKEND_ROOT` and
`LOOPZERO_FRONTEND_ROOT`; pytest collection uses `LOOPZERO_PYTEST_ROOT`,
`LOOPZERO_PYTEST_LANE` and `LOOPZERO_PYTHON`. Size policy is installed through
`loopzero.hooks.size_lint.configure`, including roots, thresholds and exemptions.
No product exemption list is installed by default. IntelFlo retains its
`fastapi_backend`, `nextjs-frontend`, and `fastapi_backend` pytest-root defaults;
other consumers must configure these roots, and missing roots fail closed.

Protected branches use `LOOPZERO_PROTECTED_BRANCHES`; skill directory wiring uses
`LOOPZERO_CANONICAL_SKILLS` and `LOOPZERO_SKILL_MIRRORS`. Commit identity uses the
namespaced `COMMIT_AUTHOR_NAME`/`COMMIT_AUTHOR_EMAIL` fields. IntelFlo retains
its fixed authorized identity; other consumers must configure both. An optional
shared-state CLI warning uses
`LOOPZERO_SHARED_STATE_COMMAND`.

The composition root supplies the approved base and candidate head explicitly
for each validation invocation. Shell fragments are integration assets, not a
substitute for that policy.
