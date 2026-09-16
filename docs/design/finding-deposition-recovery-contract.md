# Authenticated finding-deposition recovery

Status: implementation contract for coordinator inspection. No live authority
records were written and no model was launched.

Source baseline: loop-zero `71dd77746cc6eb0955a8cc87789bb6a6d0124245`.
Consumer checkpoint: IntelFlo `3de86ccbfa19fd1882db6aac4d7c4f26610a0175`.

## Problem and owner

An authenticated dispatcher can finish a formal review, persist its exact result,
then fail while depositing material findings. The terminal is correctly recorded
as `infrastructure-failure/finding-deposition-failed`, but no supported operation
can finish the already-produced result:

- `loopzero.review.evidence.append_finding_records` requires a genuine run-to-PR
  binding (`src/loopzero/review/evidence.py:371-401`).
- The kernel excludes registered-dispatcher recovery
  (`src/loopzero/kernel/authority_projection.py:1338-1367`).
- The consumer recovery path accepts only blocked/completed formal reviews and
  rejects registered authority (`scripts/util/agent_dispatch.py:12451-12528` and
  `scripts/util/dispatch_authority_runtime.py:674-707`).

The authority rule belongs to loop-zero. IntelFlo would own only a consumer
command after the package defines a native recovery and ownership transition.
A consumer-only exception would bypass the kernel projection and is not
acceptable.

## Ownership gap under the current native contract

Do not invent a PR number or use the legacy draft/checkpoint exception. #4432 is
a new `loop-zero-v1` run. IntelFlo's controlling contract explicitly forbids
checkpoint commands for new tasks and requires direct `pr_publish.py` only after
accepted review (`docs/guides/dev-workflow/loop-zero-delivery.md:23-28,95-108`;
root `AGENTS.md:91-108`). The legacy orchestration method has lower precedence.

`authenticated_delivery_run_pr` does not authenticate GitHub state itself. It
projects a unique positive `pr` from `delivery_controller_records`
(`src/loopzero/review/findings.py:362-373`). In current source, such ownership can
come from a coordinator control record, or from a registered dispatcher terminal
whose coordinator-signed attempt registration and dispatcher terminal contain
the same PR (`src/loopzero/review/authority.py:1485-1520`). Neither source exists
for a normal prepublication new-task review: checkpoints are forbidden, the PR
does not yet exist, and `pr_publish.py` requires the accepted review first.

The baseline has no supported native prepublication run-to-PR binding for this
recovery. This contract introduces a run-scoped provisional owner and a replayable
transition to the real PR created or adopted by `pr_publish.py`. The transition
preserves PR-scoped merge blocking and expiry; omitting `delivery_run_id`,
accepting `pr=None`, or trusting a future caller-supplied PR remains forbidden.

## New package admission

Add one narrow package API for a coordinator-authenticated
`attempt-recovery` with `recovery_classification=finding-deposition-only`.
Both the builder and the ledger projection must independently require:

1. Exactly one preceding registered dispatcher terminal for the same
   `(task_id, attempt_index)` and no later terminal/recovery for the work unit.
2. `read_only=true`, `work_kind=review`, `advisory!=true`, and
   `review_intent=delivery-code-review`.
3. Exact status pair
   `infrastructure-failure/finding-deposition-failed` and `deposit_state=none`.
   No other infrastructure failure becomes recoverable.
4. Valid original dispatcher proof, task-contract hash, source identity,
   snapshot commit/tree, patch identity, reservation ID, generation ID, family,
   run ID, repository binding, and result-artifact path/digest.
5. The result bytes still hash to `result_sha256`, parse as the required review
   schema, and contain complete required code/security sections. Recovery never
   changes the semantic result or creates a replacement result.
6. A coordinator-authenticated `finding-recovery-admission-v1` creates one
   deterministic provisional owner bound to the repository, run, producer task,
   attempt, result digest and reviewed snapshot. It derives those values from the
   authenticated registration and exact closed terminal; callers cannot supply
   alternate lineage. Existing checkpoint records are not eligible.
7. An exact idempotent finding-capture receipt for that pending owner, producer task,
   attempt, result and original snapshot, plus a review-chain receipt built from
   the same result and finding IDs. The receipt is validated from ledger state,
   not trusted as caller prose.
8. Coordinator authority on the recovery record and a closed original attempt.
   The API must append through the protected authority transaction; callers do
   not construct or hand-sign a record.

The authenticated projections compare the semantic admission to the original
terminal while requiring the current `ts`, `schema_version`, and
`policy_version` envelope and rejecting every unknown field. Before recovery,
the package reloads the sole capture receipt from the provisional stream and
requires byte-for-byte canonical equality with any caller reference. Caller
prose cannot replace persisted ownership evidence.

Every path-bearing finding is anchored to the blob and optional line context in
the authenticated `snapshot_sha`; the commit must resolve to the recorded
`snapshot_tree_sha` immediately before capture. If the original deposition
failed before assembling `review_chain_receipt`, the package reconstructs that
receipt only from the authenticated task contract, exact result, snapshot and
persisted finding IDs. An incomplete section contract remains unrecoverable.

The consumer seals the prospective recovery first, then retains the authority
ledger lock and the original task's attempt-lifecycle lock while the package
validates the fresh history, persisted receipt, repository identity, proof and
exact recovery projection. The consumer may bypass ordinary first-settlement
append only when that validator returns append. The same validator returns an
idempotent no-write result for the one already-authenticated recovery; any
second or changed recovery fails closed. The package never receives signing
material and the consumer never interprets recovery eligibility itself.

The authority ledger owns admission and the later unique PR binding. A distinct
provisional finding stream owns immutable content and its capture receipt. The
ordinary Finding Ledger retains positive, non-null PR scope. The recoverable
sequence is admission, provisional capture, recovery evidence, ordinary verdict,
real PR create/adopt, authenticated one-time binding, materialized PR capture,
then published evidence. Each boundary is idempotent; there is no claimed atomic
transaction across GitHub and the two ledgers.

Extend `_recovery_matches_deposit` only for this exact formal-review failure
pair. Replace the blanket registered-dispatcher rejection with a dedicated
validator for this classification; retain the rejection for every other
registered terminal. Project this classified recovery as authenticated and
evidence-eligible only when all deposition and provisional-ownership receipts
validate. It is not accepted review evidence until the ordinary
coordinator-authenticated verdict is appended.

Do not append a second reservation or settlement. The existing unresolved
settlement remains the historical result of the failed local deposition. After
the accepted recovery, the ordinary independently authorized `verdict` for the
same task makes the existing settlement project as consumed; this preserves one
primary slot and leaves the single delta slot unchanged. A crash before verdict
therefore remains unresolved rather than accidentally granting acceptance.

## Consumer command

Add a distinct `recover-finding-deposition` controller command; do not widen the
generic `revalidate` command.

Under the worktree lease and authority-ledger lock, the command must reload all
authority, validate the package admission above and rehash the result artifact.
It may proceed only after the package supplies the independently reviewed native
pending-ownership API. Recreate a
temporary detached worktree from the recorded `snapshot_sha`; verify its tree is
the recorded `snapshot_tree_sha`; use it only to anchor findings; then remove it.
The delivery checkout may already contain the repair delta and need not equal
the predecessor source. Repository common-directory identity must still match
the original terminal, so authority cannot be copied to another clone.

The current `append_finding_records(..., pr=...)` API cannot represent this
state safely. A reviewed package API first records the run-scoped provisional
capture. Before creating a PR, publication loads authenticated provisional
findings and applies the ordinary severity, state and staleness rules; missing
admission or capture receipts fail closed and open critical/important findings
block. After the publisher returns the real PR and revalidates repository, head
and base, the coordinator appends `finding-publication-binding-v1` exactly once.
A different PR is rejected permanently, including after the first PR closes.
The identical binding replay is accepted. Materialization into the positive-PR
Finding Ledger must complete before published evidence is written; metadata
failure after binding does not undo it.

The command performs no provider call, creates no attempt-start, reservation,
generation or settlement, and never changes capacity. It prints the recovered
task, original artifact digest, pending-owner identity and recovery digest,
without credential data.

## Focused regression contract

Package tests must prove:

- a registered terminal with the exact failure pair, genuine pending ownership,
  matching artifact and capture/chain receipts yields one accepted recovery;
- the original unresolved settlement projects consumed only after the normal
  authenticated verdict, with primary count one and delta count zero;
- identical retry returns the same recovery and capture operation, including a
  crash after finding fsync and a crash after authority append;
- forged result bytes/digest, result path escape or symlink, wrong source/tree,
  task/attempt/reservation/generation/family, wrong repository or run,
  forged pending owner, and wrong predecessor are rejected;
- replay under another pending owner or run is rejected, and a newer unit terminal or an
  existing conflicting recovery is rejected;
- every other failure class and any blocked/completed legacy recovery retains
  its existing policy;
- compaction retains the original dispatcher terminal, pending owner, capture
  receipt, recovery, verdict, unresolved historical settlement and effective
  consumed capacity without duplication.
- provisional ownership does not expire with time; only authenticated run
  supersession/abandonment or its unique PR binding changes its standing.

Consumer tests must prove:

- no authenticated native pending owner fails before finding-ledger or authority
  writes;
- publication binds pending ownership only to the real PR returned by the
  authenticated publisher, exactly once;
- recovery anchors against the recorded predecessor snapshot while the delivery
  checkout is on its bounded repair head;
- interruption at both persistence boundaries is idempotent;
- the command never invokes the model/runtime adapter and never creates a new
  launch/reservation/settlement;
- the preserved #4432 record can be projected as eligible, but no live recovery
  is executed by the regression.

## Current checkpoint applicability

Safe recovery of `dispatch-87cc6f3e94fa` is possible in principle, but not with
the current binaries. Its authenticated terminal has the exact failure pair,
snapshot `6bbb9953779ac8a07927455f6ffa492f4ced7569`, tree
`6dbf956f3a2f773009f8a1d1c627a18f891dc3aa`, result digest
`5c4fcb93daa70462baa72d8e0258f0979b0faf3c8ca57af7052edacd8e39cef8`,
reservation `rr_da0459cb362e398b5c8e136a0368b4ea`, and generation
`cg_408d00d271af3ea6eda931928d4dbf75`. Its result has complete code/security
sections and one material finding. It lacks any currently supported native
  prepublication finding owner in the installed binaries. Recovery is therefore
  not yet safely possible through existing commands. Required order is: independently
  review and merge this package authority and publisher transition; adopt it into
  the generated facade and consumer command; then recover, record the ordinary
  verdict, and use the already-authorized bounded delta. No live authority record
  may change before reviewed package and consumer support are installed.

## Settled design decisions

1. `finding-recovery-admission-v1`, signed by the coordinator and derived from
   the authenticated closed attempt, creates provisional ownership without a new
   task or review-capacity transition.
2. The authority ledger owns unique admission and binding; the provisional stream
   owns immutable content. Replayable transitions replace cross-system atomicity.
3. Publication checks provisional debt before GitHub mutation and fails closed on
   missing receipts. It binds only after real PR/source revalidation and
   materializes before published evidence.
4. Provisional ownership has no clock expiry. Authenticated supersession,
   abandonment, or the permanent one-PR binding changes standing. Compaction
   retains the full identity and cardinality needed to reject replay and rebinding.
