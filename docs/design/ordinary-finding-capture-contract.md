# Ordinary prepublication finding capture

Source contract for [#18](https://github.com/feder-positronics/loop-zero/issues/18),
based on `17ce7db2778b705d1004752f967098142acc9934`. This is package support;
consumer adoption and an exact-source consumer E2E remain separate acceptance.
No live record, model launch, reservation, historical review, or recovery is
created by this change. The [authority review bar](authority-review-bar.md) and
existing [failed-deposition recovery contract](finding-deposition-recovery-contract.md)
remain controlling. The failed-deposition eligibility predicates are unchanged.

## Producer proof and ownership

A signed attempt-start authenticates registration, not a later result file.
The ordinary path therefore requires a distinct dispatcher-signed
`finding-producer-completion-v1`. It has `status=completed`, the current telemetry
and policy versions, a timestamp, the exact result artifact path/digest, and the
closed identity field set shared with provisional finding ownership. The
signature must verify against the dispatcher public key in the unique preceding
coordinator-authenticated registration. A supplied digest, independent signing
key, or unsigned matching object is insufficient.

`build_producer_completion` derives the identity from existing protected records
and validates the exact result and snapshot. Its output is unsigned and confers
no authority. Only the registered dispatcher that observed execution completion
may seal it using `TerminalAuthority.seal(..., authority_kind="dispatcher")`.
The consumer must preserve those exact signed bytes before requesting capture;
losing both the original signer and its proof leaves the operation blocked,
not authorized for a replacement review.

The consumer schema was checked against IntelFlo `dbcafedac828d8454ce89a21e90228b568694e71`,
`scripts/util/agent_dispatch.py:10160-10245`. Native starts contain `run_id`,
`review_reservation_id`, `review_generation_id`, source identity, snapshot,
immutable task contract/hash, and dispatcher registration. They do not need
synthetic top-level `family`, `repository_binding`, or `delivery_contract` fields.

- Reservation and generation come from those native references and authenticated
  package review-state records. The reservation must belong to this task, remain
  unsettled, and use the protected delivery review family.
- Repository binding comes from the authenticated `ReviewGenerationV1`, never
  from a credential, source-label convention, or caller assertion.
- Task contract hashing and protected intent/family checks reuse package rules.
  The snapshot tree and patch identity must match the reserved generation.
- The original native delivery contract is read from the existing host skill-run
  log, the same boundary used by positive-PR capture. Missing, legacy, or changed
  run contracts fail closed. This does not migrate an existing run.
- Complete code/security sections, snapshot existence/tree, repository binding,
  artifact digest, and artifact path confinement are revalidated before use.

Equivalent-but-different generation snapshots are conservatively rejected by
this initial producer contract. No equivalence claim may widen its exact-source
check. Native schema support is exercised with prefixed registration fields,
separate coordinator/dispatcher keys, and package-built generation/reservation
records, without model execution.

## Integration sequence

The new APIs are exported through `review.provisional_findings` so consumers
reuse the existing finding seam.

1. After executor completion and immutable result persistence, the registered
   dispatcher builds and seals the completion proof. This proof is not a
   terminal, verdict, launch permission, or slot settlement.
2. The coordinator calls `build_capture_admission(records, repo=..., completion=...)`.
   It derives a domain-separated owner and a `finding-capture-admission-v1`
   carrying the exact signed completion and original task contract. The
   coordinator seals the admission with the ordinary envelope.
3. Under `authority_ledger_lock` and the original `attempt_lifecycle_lock`, reload
   authority and call `authorize_capture_admission_append`. Keep both locks
   through durable append; false means exact replay and no write. The validator
   independently authenticates registration, completion and prospective host
   signature. An already-closed or superseded producer cannot gain a new owner.
4. `capture_provisional_findings` reloads the exact artifact and writes the
   existing immutable provisional batch/receipt. Ordinary material selection
   uses #79's `persisted_review_findings`; suggestions do not become debt.
5. `build_capture_terminal_evidence` rebuilds the capture receipt and review-chain
   receipt from retained evidence and returns fields to join to the ordinary
   dispatcher terminal. It supplies canonical derived identities alongside the
   native prefixed fields already in that terminal. The existing dispatcher
   signs/appends its terminal, and normal verdict and slot settlement follow.
   A forged or changed capture join is rejected by both accepted-terminal and
   verdict/outcome projections. A later task verdict cannot turn the admission
   itself into a verdict-bearing record.
6. The publisher enumerates the authenticated union of ordinary and recovery
   owners. Pending capture/terminal evidence fails closed; open important or
   critical findings block publication through the existing debt predicate.
   After real PR/source revalidation, the existing binding callback binds each
   eligible owner once and materializes into the positive-PR Finding Ledger.
   Unbound findings never appear in another PR's reads. Changed PR/source
   rebinding fails; exact binding/materialization replay returns existing state.

No ordinary caller may set `pr=None` on positive-PR capture, invent a draft PR,
manufacture a failed terminal, or use the recovery classification to enter this
path. The producer's original reservation and attempt count remain unchanged.

## Persistence, retention, and compatibility

The admission embeds the producer proof, avoiding another independently mutable
completion stream. The Finding Ledger and authority ledger retain their existing
separate persistence boundaries. Exact re-entry after admission or capture
returns the original identity and receipt; conflicting evidence fails closed.
A remote PR creation followed by a binding failure uses the existing publisher
failure/reconciliation behavior, not another PR or review.

Compaction retains the original registered attempt, ordinary admission/proof,
terminal, verdict, binding, and supersession context together with existing
review-state retention. Projections preserve `AuthorityRecordView` provenance.
Neither elapsed time nor a new task label clears an owner. Signed supersession
prevents new admission; existing publication resolution/retirement rules remain
authoritative. Missing retirement evidence stays blocked, and history remains
retained rather than silently discarded.

The new admission is registered in the closed governed-family registry. Older
writers cannot safely handle it: their existing compaction guard rejects an
unknown current-policy family. Keep a compatible package reader/writer after
first use. Reverting source does not authorize deleting admission, capture,
reservation, or publication history.

## Validation and remaining acceptance

Native probes cover signature/identity forgery, post-terminal completion,
missing or changed artifacts, incomplete sections, native-run rejection,
lock-required append, pending/open publication debt, one-time binding,
materialization replay, compaction, and original-slot accounting. Additional
verdict-join probes demonstrated and prevent accidental inheritance of a later
coordinator verdict by an admission or invalid capture terminal. Original
failed-deposition recovery probes remain unchanged.

Consumer adoption must wire the protected signing/append sequence above into
its first-review path and show exact-source E2E through capture, ordinary
verdict, real publication binding and PR-scoped reads. The historical #73
recovery-only E2E is not this acceptance. #18 remains open for that work.
