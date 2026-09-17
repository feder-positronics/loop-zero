# Decision record: loop-zero executable core

<!-- Generic decisions only. Decisions that are properties of a
particular consumer's repository (its protection ruleset, environment
prefix, throughput thresholds, dependency wiring, operator staffing and
history import) are recorded by that consumer. -->

Recorded 2026-09-11 from the maintainer's answers, with later entries dated in place. Status per item: settled,
assumed (proceed unless overruled) or deferred (blocks a named step).

## Settled

| # | Decision | Consequence |
|---|---|---|
| D1 | loop-zero becomes an executable Python package; charter amended | README and contract wording change in the skeleton PR |
| D2 | Linux only | `bwrap` sole sandbox; no sandbox-runtime backend |
| D3 | Two cutovers: A mechanisms, B thin core | One-step plan withdrawn |
| D4 | Developer experience must not break | Invariants are blocking gates in both windows |
| D5 | Distribution: vendored `core/` snapshot plus package installed from the same SHA | Status tool checks both versions match |
| D6 | Python 3.14 or later; CI uses only 3.14 (updated 2026-09-17) | `uv python install 3.14` locally |
| D11 | Vendored methodology skills: `grill-with-docs`, `tdd`, `code-review` from mattpocock/skills at a pinned SHA | Other Pocock skills not vendored; superpowers not adopted |
| D14 | pytest for the package | Existing loop-zero tests kept, CI switches to pytest |
| D15 | Release branch `release/0.3` with stacked PRs; final merge is R-A | Same pattern for B as `release/0.4` |
| D17 | Rebuilt orchestration capped at 6,000 lines; deletion list is part of the design | Budget report in CI |
| D18 | State: stdlib SQLite WAL, single writer, persisted intents with idempotency keys | No LangGraph. DBOS is not rejected outright: see D25 |
| D19 | Symphony: vocabulary and error categories only, pinned revision | Lifecycle not adopted wholesale |
| D20 | Agent Client Protocol SDK deferred to the shrink step as a protocol adapter | Cursor parser moves as-is at A |
| D21 | Cutover A is MOVE, NOT REWRITE: every mechanism keeps its public behavior and its the first consumer tests; product coupling becomes configuration only | Recorded 2026-09-12 after the first A4 attempt thinned six mechanisms and was rejected; the rejecting review became the specification for the second attempt |
| D22 | Secret material never touches a shared tree. Credential staging and refresh homes live under the state root in a per-refresh private directory with exclusive creation, mode 0700, scrubbed after use. Workers receive a snapshot with the refresh capability removed. Private mounts are declared per launch in the wrapper's launch specification, never inherited from a root bound into more than one sandbox. A layout test rejects any secret path under the workspace root, repository root or any shared bind | Recorded 2026-09-12 from the PR #13 cross review; cutover B moves credentials to sealed descriptors so the child never sees a credential file |
| D23 | Cross-harness review is mandatory for any change touching credentials, sandbox mounts or authority state; a same-vendor review chain alone does not settle such a change | The PR #13 staging defect and the PR #14 home-writable defect passed several Codex rounds and were found by Claude cross review |
| D24 | Libraries added at A/B: jsonschema as a development and conformance dependency; structured stdlib logging carrying attempt, effect, candidate and epoch identifiers; OpenTelemetry stays optional. Rejected again on 2026-09-12: filelock, bubblewrap wrappers, nsjail, gVisor, pueue, pygit2, Dulwich, GitPython, Typer, Click, pydantic-settings, mandatory Pydantic | Round-2 survey in the coordinator's analysis notes |
| D25 | DBOS is a decision gate in B, not a dependency: compare one SQLite-intent implementation with DBOS on the same attempt and continuation scenario only if generic durability threatens the effects and reconciliation budget; accept only with net deletion, unchanged authority semantics, crash tests around launch, remote success, receipt write and checkpoint, and a supported single-executor topology behind an explicit extra | B3 |
| D26 | Vendor-native subprocess coordination (Codex Multi-Agent v2 subagents, Claude Code agent teams and background agents) is a declared runner capability that a coordinator brief may use inside one worker's authority; it never replaces the governed boundaries of worktree lease, sandbox, signed receipts and review budget. OpenHands Software Agent SDK joins the B decision gate for the runner lifecycle layer | Capability matrix entry at A3; evaluation with D25 |

## Assumed

| # | Assumption | Overrule by |
|---|---|---|
| S1 | Secrets scanner: `gitleaks` binary from GitHub releases, run on the filter-repo output before import | naming another tool |
| S2 | Ruleset on `main` has an empty bypass list, including for admins | asking for an admin bypass, which must then be recorded as a residual risk |
| S3 | Runtime pins for conformance are the versions installed on the owner's host at A1 start: Codex CLI 0.154.0, Claude Code and Cursor as found | supplying other versions |
| S4 | The example consumer fixture is a Python project with a fake test lane, not a copy of the first consumer | asking for a realistic second product now |
| S5 | Cutover A ships the interim review budget as a `[review]` table read by the first consumer's existing chain, not by the package | asking to wait for B |
| S6 | systemd-run transient units are prototyped as an optional supervisor for bound jobs, keeping bwrap, protected leases and signed terminal interpretation | asking not to spend the effort before B |
| D27 | Privileged copied consumer shims (closeout guard, worktree guard, job runner copies in a trusted snapshot) contain no interpreter or package selection and never re-exec: the launcher (loop-zero's closeout controller with its validated primary) chooses the interpreter, the copy uses only the interpreter already running it and that interpreter's own package, ignores environment and on-disk layout for root selection, and fails closed otherwise. A same-account artifact (JSON record, directory layout, fixed-depth ancestor) cannot authenticate a launcher. A candidate running a copy under its own interpreter gains nothing because guard authority comes from the lease descriptor and nonce. Threat model made explicit after the fourth review: candidates run inside the kernel sandbox with only their worktree writable; same-account writes to the closeout snapshot or the state directory are outside the model, and the cheap hardenings still apply: no snapshot-relative import roots, interpreter validation before any exec in the job shim, interpreter handoff by descriptor rather than a reopened pathname, venv identity by prefix and registered worktrees | Recorded 2026-09-13 from four PR A review rounds; the first consumer PR A stages 5 and 6 implement it |
| D28 | Credential rotation is cutover B work with the shape recorded in loop-zero issue #32: selection in a broker layer beneath the adapters, a typed usage-limit outcome from structured evidence only, rotation before launch or after a launch that consumed nothing, cooldown and reservations under the kernel state root rather than the ledger, declared priority with no quota guessing, one account per child, an alias in the ledger that is never derived from credential material, one pinned account for the nightly gate, and `loopzero accounts register|status|clear` replacing the operator's global switch script | Recorded 2026-09-14 from two independent designs (Claude Fable 5.1 and OpenAI gpt-6-astra) reconciled in the issue |
| D29 | A review verdict is authenticated evidence about content, keyed by patch identity, tree and required sections; it is reused at admission across proven-equivalent heads, extended once by a delta for a substantive repair, and unsuccessful execution is counted separately and never consumes a verdict slot. One-plus-one is a ledger-owned structural invariant per content generation, not a per-PR dispatch cap. the first consumer #4415 is not merged; the package carries the work (loop-zero issue on review verdicts as content facts) | Recorded 2026-09-14 from two independent strategic analyses (Claude Fable 5.1, OpenAI gpt-6-astra) reconciled on the first consumer #4409 |
| S8 | Gate A-G2 needs a live conformance mode: the A3 conformance suite fakes the process layer even for native adapters, no runtime budget setting existed, Codex and Cursor report no dollar cost, and the SDK-bundled CLIs differ from the pinned ones. A separate live suite drives the real CLIs with tiny prompts; spend control is token and turn caps plus a USD ceiling from a pinned price table with unknown cost never treated as zero; executables are pinned by path and version | asking for a different spend model or for A-G2 to be waived |

## Deferred

| # | Item | Blocks |
|---|---|---|
| F1 | Reconciliation rules for persisted intents and epoch fencing in detail | B3 |
| F2 | Signer authorization, key rotation, replay protection across epochs | B3 |
| F3 | Which Guardian admission behaviors the first consumer still needs after B | B1 |
| F4 | A real second consumer for the portability claim | after B |
| F5 | CI restructure in the first consumer: one always-running `Required checks` job that fails when a path-required job failed or did not run, so the ruleset can require it with up-to-date enforcement and the custom gates can be deleted | B |
| F6 | Consumer identity in the public package: the embedded SSH public key, GitHub login and address in the commit-author hook, the `the first consumer-v1` legacy contract default and the incident reference in the lease module are moved to configuration or scrubbed | R-A |
