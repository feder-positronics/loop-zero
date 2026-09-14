# Independent review of the executable-core and cutover plans

Reviewer: OpenAI gpt-6-astra via Codex CLI 0.154.0, reasoning effort high, read-only sandbox, 2026-09-11.
Requested model "gpt alpha" is not available on this account; gpt-6-astra is the configured default.
Prompt: adversarial review of `2026-09-11-executable-core-architecture.md` and `2026-09-11-one-step-cutover-plan.md` with the contract files as context. Output reproduced verbatim below.

---

## 1. Verdict

The direction is defensible; this plan is not ready to execute. Its biggest flaw is treating a Git cutover as an atomic transition of processes, authority, durable state, and external GitHub effects—then claiming `git revert C` reverses it. The “thin rebuild” is also contradicted by a destination map that relocates roughly 50,000 lines.

References below: **A** = [2026-09-11-executable-core-architecture.md](2026-09-11-executable-core-architecture.md); **C** = [2026-09-11-one-step-cutover-plan.md](2026-09-11-one-step-cutover-plan.md). Likelihoods are architectural judgments; the first consumer’s implementation was not supplied.

## 2. Top 7 risks ranked

1. **Rollback restores code while resurrecting stale authority.** C, “State handling at the switch” and “Cutover runbook,” preserves keys and state roots, then restores old ledgers after new work. Reverting cannot undo published PRs, merged commits, credentials already lent, or surviving workers. **Likelihood: high; impact: critical.** Introduce workflow epochs, fence old writers, inventory external effects, and rehearse rollback after actual new-workflow activity.

2. **Trusted hooks become an authority bypass.** A, “Decision,” makes repository configuration executable, while “workflow.toml extension” exposes interpreters, dependencies and dotenv paths. Checking `make` does not authenticate the Makefile or imported code. **Likelihood: medium-high; impact: critical.** Bind privileged configuration to an approved immutable revision; run candidate-controlled hooks without signing/merge credentials or writable parent Git metadata. Bubblewrap explicitly leaves security policy to its caller. [Bubblewrap security model](https://github.com/containers/bubblewrap#sandbox-security).

3. **The monolith survives behind package names.** A, “Totals,” moves 50,000 lines; Phase 4 retains a 4,456-line pipeline and roughly 3,700-line delivery controller. C’s W4 calls this “thin” without a complexity boundary. **Likelihood: near-certain under this map; impact: high.** Replace the map with a behavioral contract, explicit deletion list, dependency boundaries, and a separately budgeted orchestration implementation.

4. **“Shadow” operation creates competing production authorities.** C, “Principle: prove on the cutover branch,” delivers real PRs while the old workflow remains active. Separate branches do not separate Git refs, merge locks, databases or credentials. **Likelihood: high; impact: high.** Specify shared resource arbitration and one merge authority. Export product-only patches onto clean main-based branches, then validate and review those exact exported heads.

5. **Restart recovery duplicates or loses effects.** A, `dispatch/attempt.py`, names a state machine but specifies no durable transaction boundary across jobs, ledger writes and GitHub requests. **Likelihood: high; impact: high.** Define persisted intents, idempotency identifiers, reconciliation, fencing and crash recovery at every external-effect boundary. Add indexed status queries; an empty replacement ledger merely postpones the 208-second problem.

6. **Acceptance evidence certifies the wrong system.** C’s G3 exercises old-main CI, G5 accepts yesterday’s rebase, and runbook step 2 declares every release-pin/rebase change mechanical. The final installed package, generated configuration and CI can therefore escape representative validation. **Likelihood: medium-high; impact: critical.** Bind evidence to the complete release/configuration/runtime tuple and invalidate affected evidence when it changes.

7. **Review numbers improve while delivery gets worse.** C’s G4 counts reviews on successful shadow PRs. A hard cap can force abandonment, scope splitting or manual review outside the counter. **Likelihood: high; impact: high.** Measure all admitted tasks: total worker-hours, settlement latency, superseded work, abandoned work, owner intervention and escaped defects, alongside review dispatches.

## 3. Disagreements

**(a) One-step versus phased strangler.** I would choose phased substitution with one authoritative writer at each boundary. C, “What ‘one step’ means,” overstates the absence of dual running: its preparation explicitly runs both workflows against production resources. Given the fixed decision, retain one production switch but demand isolated preparation and a fenced activation epoch. A’s “Phases and exit criteria” must stop describing intermediate production substitutions.

**(b) Porting versus rewriting the kernel.** Port the security-sensitive mechanisms initially; rewriting them simultaneously with orchestration increases uncertainty. But A’s Phase 1 is not a minimal kernel: events, skill logs and policy-oriented hooks need justification. Preserve behavioral regression tests, inspect parameterization changes, and test hostile descendants. Existing line counts and passing inherited tests do not establish containment.

**(c) Rebuilding dispatch thin versus porting it.** Rebuild. A’s Phase 3 mostly decomposes and parameterizes the existing dispatcher; Phase 4 moves its surrounding machinery almost intact. Define a small transition function, effect executor and reconciliation loop. Preserve externally required behavior, including interrupted-work adoption; discard incidental internal APIs.

**(d) Symphony adoption.** Borrow vocabulary and timeout categories, not its lifecycle wholesale. Symphony §7 includes continuation retries and tracker-driven recovery without a durable orchestration database; §10.6 describes timeouts, not governed delivery. Those semantics do not establish review budgets or exact-head merge authority. Pin the specification revision and document every adopted and rejected behavior. [Symphony specification](https://github.com/openai/symphony/blob/main/SPEC.md).

**(e) ACP as a runner.** Keep ACP, but call it a protocol adapter, not a complete runtime replacement. C’s W3 must test authentication, cancellation, permission denial and blocking extensions: Cursor explicitly requires responses to permission and certain extension requests. [Cursor ACP documentation](https://cursor.com/docs/cli/acp). The Codex SDK assumption is valid, but its published Python builds bundle a pinned runtime; reconcile that with C’s separately pinned CLI. [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk).

**(f) Pocock versus custom versus superpowers.** Custom governance plus selected vendored methodology is reasonable. C’s W5 needs an explicit skill allowlist, overlay precedence and behavioral evaluations; rendering proves only packaging. Rejecting superpowers as a plugin is sensible because its documented task workflow adds two-stage reviews, potentially recreating the measured bottleneck. [Superpowers workflow](https://github.com/obra/superpowers#the-basic-workflow). Do not replace that with an equally compulsory custom ceremony.

**(g) Private-repo merge gate.** I would reopen the hosting-plan decision before accepting permanent maintenance of A’s 2,856-line compensating control. Native private-repository branch protection exists on paid plans. [GitHub protection availability](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches). If retaining the gate is mandatory, define its bypass boundary: a cooperative lock cannot constrain another credential that can directly update `main`.

**(h) SQLite versus DBOS/LangGraph.** Neither document actually chooses SQLite, DBOS or LangGraph. That omission matters more than library preference. For one Linux host, I would begin with stdlib SQLite, explicit transactions and reconciliation; WAL allows concurrent readers but only one writer and excludes network filesystems. [SQLite WAL](https://www.sqlite.org/wal.html). Reconsider DBOS if durable execution becomes substantial custom infrastructure. LangGraph needs a demonstrated graph/checkpointing requirement, not merely the presence of agents.

**(i) Policy versus code as review-cost driver.** A’s “Risks” says throughput improves *only* through review-budget enforcement. Unsupported. Ninety percent review time identifies where spending occurs, not why; supersession, slow evidence queries and repeated invalidation are implementation effects too. Enforce the existing contract’s budget, instrument dispatch reasons, and separate policy, implementation and interaction costs before claiming causality.

## 4. Missing

- **Authority semantics:** signer authorization, key rotation/revocation, replay protection across ledger resets, independent checkpoint anchoring, and the distinction between authentic events and truthful evidence.
- **Operations:** disk exhaustion, partial writes, reboot recovery, orphan descendants, API uncertainty, backup restoration, log redaction and retention.
- **Governance:** A’s durable `review/findings.py` needs reconciliation with `core/CONTRACT.md` and `core/HANDOFF.md`, which prohibit parallel finding backlogs and evidence authorities.
- **Testing:** explicit fault scenarios and behavior-preservation tests for rewritten modules. C’s instruction to discard tests with unported code risks discarding the only specification of required behavior.
- **People and provenance:** a second rollback operator, maintenance ownership, and inspection of *historical* secrets/private content before C’s `filter-repo` import. File selection alone does not sanitize history.
- **Specification consistency:** A leaves hub handoff consumer-owned; C deletes it. C adds `init`, `sync` and `doctor`, while A’s CLI surface does not define them.

## 5. Simplifications

Cut the optional CI review route, unused native-protection implementation, general tracker abstraction and broad SDK version matrix from this release. Support the first consumer’s pinned configurations first.

Move only required Guardian behavior behind a narrow consumer interface; defer generic Guardian packaging. Vendor only methodology skills actually invoked.

Keep consumer CI definitions and add a small loop-zero interface instead of simultaneously replacing every workflow with reusable CI. Full workflow substitution does not require generic CI ownership.

Preserve historical provenance through a private archive and mapping manifest if importing history threatens scope or confidentiality. Keep a second-consumer portability claim separate from the first consumer substitution.

## 6. The cutover runbook

The gates in C, “Gates before the window opens,” need these changes:

- **G1:** Add installed-artifact tests, locked dependencies and containment tests. Source-tree tests are insufficient.
- **G2:** Replace “three green nights” as the sole criterion with mandatory scenario coverage: expiry, denial, cancellation, disconnect, restart and malformed output. Record the exact runtime tuple.
- **G3:** Keep representative deliveries, but add concurrent ownership and interrupted delivery. Old-main CI cannot certify replacement CI.
- **G4:** Require predeclared settlement, waste and quality thresholds across all attempts. Benchmark the historical 182 MB ledger workload and a larger fixture.
- **G5:** Replace “within one day” with validated candidate/base identities held stable through merge.
- **G6:** Close admission first. Empty registries do not prove absent processes, timers, remote CI or stale credentials.
- **G7:** Rehearse stateful rollback after new runs, new ledger entries and a published PR, using isolated credentials and resources.
- **G8:** Require approval of the concrete release tuple, residual risks and demonstrated restoration time.

C’s “State handling” also needs precision. **Ledgers:** archive outside the active directory and record an epoch boundary. **Runs:** verify process termination, not just inventories. **Findings:** do not merge unsafe work merely to drain; preserve unresolved ownership when closing PRs. **Worktrees:** adoption must retain dirty and untracked files. **Keys:** retaining location does not establish replay safety. **Credentials:** specify revocation and descendant-FD handling.

For “Cutover runbook” itself:

- **T−1:** Identify every launcher and timer; define who closes admission and who can restore it.
- **Step 1:** Publish R before opening the window, as “What ‘one step’ means” already requires. Run final release gates against R.
- **Step 2:** Remove “Any change here is mechanical.” Rebase conflicts and package/configuration changes require impact assessment and appropriate revalidation.
- **Step 3:** Fence admission and acquire the cutover lock before confirming drain. Snapshot state and checkpoint digests here.
- **Step 4:** Validate the intended integration tree while preventing base movement.
- **Step 5:** Record the resulting squash SHA and reconcile it with the validated tree and evidence.
- **Step 6:** Install from cached, verified artifacts; reload units explicitly. Define `doctor`’s checks before relying on it. Keep broad admission closed during the canary.
- **Step 7:** Lift the freeze only after canary settlement and authority checks.

**Rollback:** `git revert C` is one operation, not the recovery procedure. Stop admission, fence new workers, preserve dirty work, inventory GitHub effects, archive new state, restore the old environment and units, reconcile surviving PRs, then run a canary. Never blindly reactivate stale leases. Authority or sandbox failure demands immediate containment, not a one-hour repair allowance; recovery capability must also exist after forty-eight hours.

## 7. Three questions the owner must answer before starting

1. **What are the quantified success thresholds** for settlement, total worker-hours, supersession, status latency and escaped defects—and do abandoned tasks count?
2. **What is the authoritative state and fencing model** across old/new workflows, crashes, GitHub effects and rollback, and who besides the implementer can restore service?
3. **Which document controls scope:** the 50,000-line port or the thin rebuild—and exactly which behaviors must survive, including findings, Guardian, hub handoff and merge authority?