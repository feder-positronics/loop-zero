# Executable core: architecture and migration map

Status: revised proposal, 2026-09-11, after the external review recorded in
`2026-09-11-review-gpt-6-astra.md`. Source inventory: the first consumer at commit
`c621b1751`, classified module by module. Line counts are from that commit;
cluster sizes inside `agent_dispatch.py` are estimates.

## Decisions

- loop-zero grows from a prose contract into a prose contract plus one Python
  package, `loopzero`. Consumers keep the vendored `core/` snapshot and install
  the package from the same full commit SHA.
- Linux only. `bwrap` is the only sandbox backend.
- Distribution: the `core/` prose snapshot stays vendored; the package is
  installed by a uv git dependency at the same full SHA. Python 3.12 or
  later; tests run under pytest.
- the first consumer's GitHub organization is on the Team plan (verified 2026-09-11).
  Rulesets are available; none are configured yet. Configuring them is a
  pre-A task, and the private-repository merge gate is not ported.
- Two cutovers, not one. Cutover A moves mechanisms with behavior preserved.
  Cutover B replaces the first consumer's orchestration with a thin core. See
  `2026-09-11-two-step-cutover-plan.md`.
- **Scope rule.** The thin rebuild controls. A file is moved and generalized
  only if its parameters fit in `workflow.toml` and its internal imports are
  already in the package. Anything needing a large parameter object or
  pulling ten sibling modules is orchestration policy and is rebuilt. The
  deletion list below is part of the design, not an afterthought.
- **Complexity budget.** The rebuilt orchestration (attempt state machine,
  review budget, delivery continuation, effect executor, reconciliation) is
  capped at 6,000 lines excluding tests. Exceeding it is a design failure,
  not a reason to raise the cap.
- The README line "there is no dispatcher, task runner or installer" becomes:
  the core ships a dispatcher and delivery state machine as a library, and a
  generator for consumer wiring; it ships no scheduler service, dashboard or
  cloud control plane.

## Authority of configuration and hooks

The contract rule that `workflow.toml` is never executed narrows as follows:

- The package executes only hook commands declared under `[hooks]`, after the
  trusted-executable check, and never anything interpolated from an issue, PR
  body or other untrusted text.
- Privileged hooks (acceptance, setup, closeout) are read from the **approved
  base revision** of `workflow.toml`, not from the candidate worktree. A
  candidate that changes `[hooks]` runs under the base policy until that
  change is merged. This closes the bypass where a candidate edits its own
  acceptance command.
- Hook processes run as validation children: no signing key, no merge or
  commit credentials, no writable parent Git metadata, inside the sandbox.
  This is the contract's existing "validation children" section applied to
  hooks explicitly.

## Layers

| Layer | Package path | May know | Must not know | Dependencies |
|---|---|---|---|---|
| L0 contract | `core/` | Policy, roles, evidence | Tools, commands | none |
| L1 kernel | `loopzero.kernel` | Git, filesystem, processes, hashes, signatures | Agents, products, GitHub | stdlib, `openssl`, `bwrap` |
| L2 runners | `loopzero.runners` | One agent protocol per adapter | Products, review policy, Git authority | one optional SDK per adapter |
| L3 mechanisms | `loopzero.review`, `.delivery`, `.integrations` | Acceptance, review evidence and authority, merge and CI gates, GitHub | Product paths, check names, aliases | L1, L2 |
| L3 orchestration | `loopzero.dispatch` | Attempt lifecycle, routing, review budget, delivery continuation | same | L1, L2, mechanisms |
| L4 consumer | consumer repo | Everything local | Nothing shared | product choice |

## Findings that shaped the design

- The inventory is stdlib-only except PyYAML in the Guardian ratchet and
  Typer in the product-only runtime owner. Base install has no third-party
  dependencies.
- Ed25519 signing uses an `openssl` subprocess with key material over memfd.
  Kept; no `cryptography` dependency.
- The delivery contract id `loop-zero-v1` already exists beside `the first consumer-v1`
  in fifteen modules. The package keys off it; `the first consumer-v1` is not ported.
- Fourteen of twenty-two dispatch and runtime files couple to the first consumer only
  through the `INTELFLO_` prefix and temp names. One `env_prefix` setting
  removes that.
- Three files hard-code the backend venv interpreter and bridge path. One
  injected `toolchain` object removes all three.
- Every module injects its own directory onto `sys.path`; tests load modules
  by path. Package conversion is repo-wide and mechanical, so it is step 0.
- The hash-chained ledger is a 1,227-line leaf. One canonical-JSON digest
  helper extracted from the finding ledger frees the signed-ledger core.
- The sandbox module named after Guardian is imported by fifteen non-Guardian
  modules. It is kernel code under a neutral name.
- `pr_merge_gate.py` and `final_ci_gate.py` are compensating controls for a
  private repository without native protection. A ruleset on `main`
  now enforces pull requests, squash merges and the `PR Policy` context.
  It does not yet replace the gates: GitHub counts skipped jobs as passing and
  the first consumer's CI skips by path, so exact-head path-scoped enforcement remains
  in the custom gates until CI gains an always-running aggregate gate job
  (deferred item F5). Merge queue remains unavailable on Team. Neither module
  is ported; both stay in the first consumer until B and are deleted then.
- Guardian has a generic core (state stream, audit deposit, risk word list,
  ratchet engine) and product adapters (metrics source, delivery toolchain
  edges, systemd units, repository identity).

## Durable state

- Attempt and delivery state live in stdlib `sqlite3` in WAL mode on local
  disk, one writer process guarded by a file lock, readers unrestricted.
  Network filesystems are unsupported.
- The signed kernel ledger remains the authority record. SQLite holds
  projections rebuilt from the ledger and the transient attempt state.
- Every external effect (GitHub write, job launch, worker spawn) is preceded
  by a persisted intent with an idempotency key, and followed by a recorded
  outcome. Recovery replays intents without outcomes through reconciliation,
  never by re-running blindly.
- Status queries are index lookups. The historical 182 MB ledger and a larger
  synthetic fixture are benchmark cases in CI.
- DBOS or LangGraph are reconsidered only if durable execution becomes
  substantial custom code. Neither is a dependency now.

## Findings and the contract

`review/findings.py` keeps PR-scoped finding records that expire at merge, as
`core/CONTRACT.md` requires. It is an operational record inside the event log,
not a backlog. The audit-campaign backlog ceiling and campaign drain logic in
the first consumer's finding ledger are product policy and stay in the first consumer.

## Package layout and CLI surface

```
loop-zero/
  core/                          L0, vendored unchanged
  src/loopzero/
    config.py                    workflow.toml schema, Profile object, base-revision loading
    kernel/                      L1
    runners/                     L2
    review/  delivery/  integrations/   L3 mechanisms
    dispatch/                    L3 orchestration (rebuilt, budgeted)
    guardian/                    optional extra, generic core only
    hooks/                       shipped hook scripts, settings fragment generator
    cli.py
  tests/unit/  tests/conformance/  tests/example_consumer/
  docs/design/
```

CLI subcommands, all defined here so the cutover plan can reference them:
`init`, `sync [--check]`, `doctor`, `policy lint`, `hooks check`,
`worktree {claims,list,prune,lease}`, `job {start,wait,reconcile}`,
`ledger {status,verify,compact,seal}`, `dispatch {route,preflight,run,verdict,
supersede,abort,handoff,terminal-receipt}`, `review {preflight,chain,evidence}`,
`delivery {ready,continue,settle,status}`, `evidence {render,verify}`,
`checks`, `status`.

Optional extras: `claude`, `codex`, `acp`, `guardian`. Python 3.12 or later.

## Destination map

Verdict legend: U universal, P parametrize, R replace with library, N new
code, S stays in consumer, D deleted.

### Cutover A: mechanisms moved, behavior preserved

No code from `agent_dispatch.py` moves in cutover A. the first consumer's monolith stays
whole and imports the package.

Kernel:

| Target | Source (`scripts/util/`) | Lines | Verdict | Parameters |
|---|---|---|---|---|
| `kernel/canonical.py` | digest helper from `finding_ledger.py` | ~60 | U | none |
| `kernel/ledger.py` | `dispatch_ledger.py` | 1,227 | U | domain separator |
| `kernel/authority.py` | `dispatch_authority.py` | 1,183 | P | state root, scheme namespace; legacy pubkey literal dropped |
| `kernel/authority_store.py`, `authority_projection.py` | `dispatch_authority_store.py`, `_projection.py` | 3,124 | P | policy and telemetry version strings |
| `kernel/ledger_lifecycle.py` | `dispatch_ledger_lifecycle.py` | 1,581 | P | audit root, caps, barrier filename |
| `kernel/worktree_lease.py` | `worktree_guard.py` | 498 | P | env prefix, lock names, timeout |
| `kernel/worktree_claims.py` | `worktree_claims.py` | 637 | P | registry dir, run-id regex, work-item id |
| `kernel/worktree_inventory.py` | `worktree_list.py`, `worktree_prune.py` | 216 | P | owned prefixes, harness tag map |
| `kernel/sandbox.py` | `guardian_sandbox.py` | 603 | P | boundary env names, mount roots, credential broker, passthrough allowlist |
| `kernel/trusted_exec.py` | `trusted_executable.py` | 74 | U | none |
| `kernel/jobs.py`, `kernel/job.sh` | `job.sh`, `job_store.py` | 3,275 | P | env prefix, state root, timeout; final-CI repro case becomes a consumer hook |
| `kernel/events.py` | `agent_event.py`, `skill_run_identity.py` | 1,066 | P | log root, contract id |
| `kernel/run_log.py` | `skill_run_log.py` | 1,575 | P | contract id, phase-skill set, log root |
| `kernel/liveness.py` | `delivery_liveness.py` | 522 | U | audit dir |
| `kernel/gitscope.py` | `dispatch_common.py`, `patch_identity.py`, `git_config_security.py` | ~700 | U | audit dir |
| `hooks/` | `primary-collision-check.sh`, `agent-bash-guard.sh`, `staging-base-check.sh`, `commit-author-check.sh`, `skill-invoke-event.sh`, `check-skills-canonical-dir.sh`, `size_lint.py`, `ci-mirror-check.sh`, `pytest-config-check.sh` | ~1,250 | P | bypass footer, protected branches, identity, thresholds, lanes |

Runners:

| Target | Source | Lines | Verdict | Notes |
|---|---|---|---|---|
| `runners/contract.py` | `agent_runtimes/contracts.py`, `governed_result.py`, `registry.py` | 629 | U | error categories borrowed from Symphony vocabulary, pinned revision |
| `runners/process.py` | `agent_runtimes/process.py` | 767 | P | three env names |
| `runners/claude.py` | `claude.py`, `claude_credential.py`, `claude_token.py` | 2,277 | P | toolchain object |
| `runners/codex.py` | `codex.py`, `codex_credential.py`, `codex_isolation.py` | 2,264 | P | toolchain object; `codex_refresh_bridge.py` D |
| `runners/bridge.py` | `sdk_bridge.py` | 2,313 | P then R | moved as-is in A; private SDK shims replaced in step 3 |
| `runners/cursor.py` | `cursor.py`, `cursor_credential.py` | 707 | P | moved as-is in A; ACP adapter added in step 3 as a protocol adapter, with permission, cancellation and disconnect tests before it replaces this |

Mechanisms:

| Target | Source | Lines | Verdict | Parameters |
|---|---|---|---|---|
| `review/acceptance.py` | `dispatch_acceptance.py`, `_grammar.py` | 1,883 | P | `[toolchain]` |
| `review/routing.py` | `dispatch_routing.py` | 654 | P | `[routing]` tables |
| `review/preflight.py` | `review_gate_preflight.py` | 163 | P | evidence evaluator, risk classifier |
| `review/chain.py` | `review_chain.py`, `review_tree_coverage.py` | 896 | P | required sections |
| `review/authority.py` | `dispatch_review_authority.py`, `dispatch_archived_review.py` | 1,464 | U | |
| `review/evidence.py` | `dispatch_review_evidence.py` | 851 | U | |
| `review/risk.py` | `delivery_review_risk.py`, `security_review_scope.py`, `ci_path_classifier.py` | ~1,200 | P | `[path_classes]` |
| `review/harness.py` | `cross_harness_review.py` | 1,024 | P | runner registry replaces hard-coded CLI shapes |
| `review/findings.py` | `finding_ledger.py` minus digest and campaign logic | ~2,200 | P | ledger dirs, severity vocabulary, security patterns |
| `delivery/publish.py` | `pr_publish.py`, `pr_publish_*`, `pr_body_check.py` | ~3,000 | P | labels, body contract |
| none | `pr_merge_gate.py`, `final_ci_gate.py` | 5,117 | S then D | replaced by a ruleset on `main`; deleted at B |
| `delivery/closeout.py` | `pr_closeout.py` | 936 | P | temp prefix, snapshot path, trusted bin dir |
| `delivery/reentry.py` | `delivery_review_reentry.py` | 120 | U | |
| `delivery/gates.py` | `core/tools/checks.py` | 88 | U | existing |
| `integrations/github.py` | extracted from the above | N | N | gh wrapper, repo identity, labels, lock, ref namespace, version floor |
| `guardian/state.py`, `deposit.py`, `risk.py`, `ratchet.py` | `guardian_state.py`, `guardian_audit_deposit.py`, `guardian_risk.py`, `guardian_tick.py` engine | 2,776 | U/P | slot schedule, manifest path |

Cutover A moves roughly 40,000 lines plus their tests. It is not thin, and
does not claim to be.

### Cutover B: orchestration rebuilt, monolith deleted

| Target | Replaces | Lines replaced | Budget |
|---|---|---|---|
| `dispatch/attempt.py` | `agent_dispatch.py` clusters 7, 11, 12 (admission, task loading, run core) | ~4,300 | 1,500 |
| `dispatch/effects.py` | scattered GitHub, job and worker launch calls | n/a | 800 |
| `dispatch/reconcile.py` | `dispatch_lifecycle.py`, `dispatch_authority_runtime.py`, recovery parts of clusters 13 to 15 | ~5,100 | 800 |
| `dispatch/brief.py`, `execute.py`, `result.py`, `scope.py` | clusters 2 to 5 | ~3,650 | 1,500 |
| `dispatch/commands.py` | clusters 13 to 19, `agent_dispatch_host.py` | ~5,000 | 700 |
| `delivery/pipeline.py`, `delivery/control.py` | `delivery_pipeline.py`, `delivery_control.py`, cluster 18 | ~8,000 | 700 |
| Total | | ~26,000 | 6,000 |

Behavioral contract for the rebuild, the only things that must survive:
task admission with lease and scope checks; hash-bound briefs with a
completion boundary; sandboxed execution with dispatcher-owned acceptance;
schema-validated results and failure classes; interrupted-writer adoption from
a terminal receipt; one review plus one bounded delta enforced by the state
machine; tier-zero classification by path class; candidate freeze, review,
verdict, publish, closeout continuation; exact-head evidence rendering; abort
and supersede with recorded reasons. Everything else in the replaced modules
is incidental and is not preserved.

Credential handling in B (from the PR #13 cross review, decision D22):
secret material is never written into a tree bound into more than one
sandbox. The host broker serves credentials over a sealed memory descriptor
or an inherited descriptor through the kernel's credential-lending path, so
the child never sees a credential file it could copy. Refresh runs only in
the broker under the renewal lock, in a per-refresh private directory under
the state root, and the worker snapshot carries no refresh capability. Any
launch-specific mount is declared in the launch specification the sandbox
wrapper receives; nothing private is inherited from a shared root. Until the
vendor CLIs accept descriptors, the staged home stays, under those rules.

### Deletion list

Deleted at cutover B, never generalized: `pr_merge_gate.py`,
`final_ci_gate.py`, the merge lock branch, `agent_dispatch.py` including the
facade re-exports, `agent_dispatch_host.py`, `dispatch_lifecycle.py`,
`dispatch_authority_runtime.py`, `delivery_pipeline.py`,
`delivery_control.py`, the Tier-B experiment and its telemetry, all
`the first consumer-v1` branches, merge train remnants, the `hub/` handoff, the
trust-manifest verification loop, `test_agent_dispatch.py` and the tests of
every other deleted module. Their required behaviors are re-specified as
conformance scenarios before deletion, so the test file is not the only
specification when it goes.

### Stays in the first consumer

`worktree_setup.py`, `runtime_owner.py`, `process_doctor.py`,
`guardian_dispatch.py` admission and `guardian_delivery.py` toolchain edges,
`guardian_metrics.py`, `guardian_gate_b*.py`, `guardian_timer_install.sh`,
`commit-auto-fix.sh`, `preflight.sh`, product hooks, the nine product skills,
the audit-campaign logic. `t3_stall_detector.py` moves to the T3 tooling.

### Totals

| Bucket | Approx. lines |
|---|---|
| Cutover A: moved with injection | 40,000 |
| Cutover B: replaced by budgeted rebuild | 26,000 replaced by at most 6,000 |
| Deleted outright at B | included above plus ~37,000 lines of monolith tests |
| Stays in the first consumer | 12,000 |

## workflow.toml extension

```toml
[core]
repository = "..."
revision = "<full sha>"
path = "vendor/loop-zero"

[package]
env_prefix = "INTELFLO"
audit_root = ".audit"
state_root = "~/.local/state/the first consumer"
contract = "loop-zero-v1"
epoch = 1                       # bumped at each cutover; stamped on every record

[toolchain]
interpreter = "fastapi_backend/.venv/bin/python"
shared_artifacts = ["fastapi_backend/.venv", "nextjs-frontend/node_modules"]
dotenv = "fastapi_backend/.env"
db_url_vars = ["TEST_DATABASE_URL"]
db_lock = "/tmp/the first consumer-testdb-5433.lock"
db_targets = ["test-be", "test-be-integration"]

[routing.aliases]
sol = { runner = "codex", model = "gpt-5.6-sol", write = true }
opus = { runner = "claude", model = "claude-opus-5", write = false }
auto = { runner = "cursor", model = "auto", write = true }

[routing.tiers]
S-cross-review = { alias = "opus", effort = "high", read_only = true }

[review]
max_reviews_per_pr = 1
max_delta_reviews = 1
required_sections = ["acceptance", "risk", "gates"]

[github]
native_protection = true          # ruleset on main: PR required, required contexts, up to date
workflow = ".github/workflows/ci.yml"
labels = { in_progress = "in-progress", blocked = "blocked", standalone = "standalone" }

[github.check_commands]
"Backend Tests" = "make test-be"
"Frontend Tests" = "pnpm -C nextjs-frontend test"

[path_classes]
backend = ["fastapi_backend/**"]
frontend = ["nextjs-frontend/**"]
docs = ["docs/**", "*.md"]
generated = ["nextjs-frontend/lib/api/generated/**", "fastapi_backend/openapi.json"]

[hooks]                          # read from the approved base revision
worktree_setup = ["make worktree-setup"]
acceptance = ["make test-be-unit"]
db_acceptance = ["make test-be-integration"]
```

## Steps and exit criteria

0. **Skeleton.** Package conversion, `config.py`, `cli.py`, `init` and
   `sync --check`, example consumer, CI matrix, install by SHA. Exit: example
   consumer installs from a SHA and `sync --check` is stable.
1. **Cutover A.** Move the cutover-A tables with their tests. the first consumer's
   monolith imports the package; no observable behavior change; same ledger
   formats and paths. Exit: the first consumer gates green, containment tests green,
   one canary delivery, and the review budget enforced by configuration in the
   existing chain as an interim measure. Ruleset on `main` live before the
   window, generated from `[checks].required`.
2. **Cutover B.** Rebuild orchestration within budget; conformance scenarios
   cover the behavioral contract; delete the monolith. Exit: predeclared
   thresholds met on all admitted tasks, not only successful PRs.
3. **Shrink.** Replace the SDK bridge's private shims with public SDK
   surfaces, add the ACP adapter behind the runner contract, retire the Cursor
   parser after adapter tests pass. Continuous, no cutover.

## Non-goals

No scheduler service, dashboard, cloud control plane, Herdr, Gas City,
sandbox runtime, DBOS or LangGraph. T3 Code remains the interactive surface.
The hub handoff is deleted at B, not made generic.

## Risks

- **Monolith survives under new names.** Guarded by the scope rule, the
  deletion list and the 6,000-line budget.
- **Vendor SDK churn.** Adapters are the only importers; CI pins one version
  per adapter and tests the pinned tuple, not a matrix.
- **Throughput.** Cutover A changes nothing observable; the interim review
  budget in configuration is the only throughput lever before B. Dispatch
  reasons are instrumented at A so policy, implementation and interaction
  costs can be separated before B claims causality.
- **Authority.** Hooks read from the base revision; signer authorization, key
  rotation and replay protection across epochs are specified before B.
- **Kernel is Linux-only by decision** (`fcntl`, `memfd_create`, `bwrap`).
- **Single operator.** The owner has accepted that no second operator exists
  for B's rollback; recorded in the decision record, B-G9 waived by merge
  authority.
