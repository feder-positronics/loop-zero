# Authenticated changed-source generation links — issue 67

Status: implementation candidate; independent authority review and supported
consumer adoption are required before any live use.
Owner: https://github.com/feder-positronics/loop-zero/issues/67
Source baseline: 2e5b67345130ffa41614cebb0832133ce9414eb8.
The task fast-forwarded from f16e895; upstream managed-Python and CI repairs do
not overlap this producer. Original failure evidence remains at f16e895.

## Scope and audit decisions

The producer is package-owned (`review/lineage.py::admit_linked_delta`). The
consumer coordinator owns protected loading, exact-state signing and durable
append. This implementation handles only a substantive delivery delta from a
native original primary. It rejects legacy bridging, trust-family launches,
carry endpoints, later successors and released-delta retries. Those conservative
refusals do not grant a replacement primary. No policy choice requires expanding
this repair. The original 440-line IntelFlo deposit remains inadmissible under
the unchanged 300-line bound; its approved semantic split is not a budget reset.

The audit's four concerns are resolved as follows:

1. **Spent delta:** do not rely on `inherited_delta_consumed`. The producer scans
   all projected generations in the predecessor lineage and refuses if any has
   consumed delta authority or any delta reservation, including released or
   unresolved ones. This check applies on every transaction, including reuse of
   an existing edge. Existing admission policy has tests that intentionally allow
   later-generation reviews; it is unchanged. The new producer does not expose
   those paths or enable another hop.
2. **Older primary/forks:** only the original native primary is eligible. A
   durable successor link owns that successor even before generation/admission
   rows exist. Required sections cannot drop any section required by the original
   primary. A different successor, a descendant, conflicting predecessor, or
   duplicate physical edge is refused. An exact single edge can be retried only
   before a delta reservation exists. No selection by record recency is authority.
3. **Content versus launch:** LinkV1 remains a reusable content relationship,
   without run/family fields. It cannot authorize a launch. The transaction binds
   the actual accepted terminal to the same run, delivery intent, source ref,
   original native generation, consumed primary settlement and terminal digest.
   Separate family budgets remain package-owned. This producer never interprets
   an edge or a different run/task label as new capacity. Protected
   compaction retains the state used by these checks; authenticated views must
   not be flattened into plain lists.
4. **Git and interruption:** acquire the worktree writer lease before the ledger
   lock. Capture and compare full V2 source identity under both; require clean
   source, matching snapshot tree, and recomputed clean-HEAD patch identity.
   Snapshot commits are synthetic and need not equal the branch HEAD. Require
   task worktree and authority repository to share the kernel-derived repository
   binding; independent clones cannot borrow authority. Repeat
   immediately before link/admission append and after admission append. Re-derive
   from reloaded authenticated records after the link append. A persisted slot
   whose caller was interrupted remains held; a duplicate retry cannot launch.

The writer lease coordinates compliant writers; it is not a filesystem security
boundary against an uncontained adversary. Existing sandbox/ownership boundaries
remain required. Tests deliberately mutate source inside storage callbacks to
verify fail-closed behavior at these boundaries.

## Package/controller transaction

`admit_linked_delta(repository, *, worktree, task, expected_source, snapshot, load_records,
append_records, required_sections)` owns the lease/lock transaction. Call it from
the host coordinator before a changed-source delta launch, outside any existing
ledger lock (lock order must not invert). Its storage callbacks are trusted host
adapter functions, never task JSON or candidate code. No signer or caller-built
link is accepted as task input.

Inside the transaction:

1. Reload the authenticated record view. Capture repository binding from Git and
   compare the leased worktree and any protected accumulator binding. Recheck
   the captured binding on every source check. Locate one accepted terminal by
   the snapshot locator and run. Authenticate the exact native primary settlement.
2. Derive a substantive content edge from recomputed Git identities. Enforce
   original base, actual nonzero delta, the existing cumulative line cap and pair
   patch byte cap. Refuse all conflicting lineage capacity and successor edges.
3. For a new edge only, revalidate source and ask the host adapter to seal and
   durably append exactly the derived LinkV1 record. The consumer must use its
   exact package-state writer; generic telemetry envelopes add fields that LinkV1
   correctly rejects. Never expose a candidate-facing arbitrary-record signer.
4. Reload after append (including any compaction), re-derive and require exactly
   one authenticated matching edge. Call unchanged admission with `requested`
   fixed to `delta`. Only a new delta reservation is eligible to continue.
5. Revalidate source, durably append admission rows, revalidate again, then reload
   and verify every requested row is authenticated and durable, the projected
   generation and coverage digest match, and the exact reservation is outstanding
   and unsettled. Finally recheck source/binding after that reload. Only then
   return dispatch permission; returned rows-to-append are empty because this
   transaction already persisted them.

A crash after link fsync but before admission leaves no reservation and retries
use that edge without appending another. Partial generation/coverage appends
are reloaded and completed by admission. Once a reservation is durable, retries
are blocked, even if the original caller never received its return value. The
existing reconciler owns unfinished attempts; this producer cannot recycle them.

The protected host writer separately owns its active-file/host-head transaction:
a tail not committed to protected state must be recovered by the existing ledger
recovery mechanism. The package producer does not replace or bypass that writer.
The source/test worktree never writes live records.

## Evidence and remaining review

`tests/unit/review/test_generation_link_integration.py` uses real Git identities,
coordinator signatures, registered dispatcher signatures and consumed settlements.
Authentication is not mocked. The first admission regression failed with
`missing-evidence`; the audit regressions additionally demonstrated renewed
capacity after a consumed delta and through an older-primary fork on the baseline.

The persistent test adapter fsyncs signed JSON rows and reloads them at every
boundary. It injects interruption after link, generation, coverage and reservation
writes, and mutations during load/link/admission append. A separate integration
case runs real protected ledger compaction and proves consumed capacity survives
through `AuthorityRecordView`. These tests do not claim to exercise the consumer's
protected host-head writer crash hooks; those remain covered by that writer's
owning suite and must be validated when its thin adapter is connected.

Before live adoption, freeze the exact package and consumer integration source,
run applicable gates and obtain the required independent Claude/GPT authority
review with actual Claude execution and reviewer-run negative probes. The static
design audit is not an implementation review receipt. Existing IntelFlo Sol
coverage remains at its original source/generation and cannot count as Claude.

No frozen IntelFlo deposit, PR #4428, live run record or installed/vendor source
is modified. After accepted source review and supported adoption, use preserved
lineage for the bounded A delta only; B remains separately owned dependent scope.
