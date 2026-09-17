# Preventing recurring delivery blockages

Status: proposed direction, 2026-09-16. This assessment guides existing issues;
it creates no new authority, automatic retry permission, or parallel backlog.

The [migration and simplification exploration](2026-09-17-workflow-migration-simplification.md) develops the removal and convergence direction under #105. This document retains its narrower prevention/recovery scope and existing issue owners.

## Outcome and evidence

A coordinator should know whether work can finish before admitting it, and a
replacement coordinator should recover an interrupted operation without losing
credentials, duplicating a launch, or buying the same review again. Operators
should receive a specific action only when the remaining choice belongs to them.

The open issue count hides different kinds of unfinished work:

| Evidence | Failure mechanism | Prevention and existing owner |
| --- | --- | --- |
| [#43](https://github.com/feder-positronics/loop-zero/issues/43), [#61](https://github.com/feder-positronics/loop-zero/issues/61) | Tests import mutable developer checkouts; CI skips the comparison | Pinned package-owned reference fixtures; consumer checks explicitly opt in to a revision |
| [#21](https://github.com/feder-positronics/loop-zero/issues/21) | A cleanup test assumes scheduling and process enumeration finish within a tiny grace | Assert cleanup with a bounded load-tolerant deadline; test deadline enforcement separately |
| [#63](https://github.com/feder-positronics/loop-zero/issues/63) | A successful provider rotation can be discarded because its access lifetime misses the requested horizon | Persist validated host rotation separately from permission to export a snapshot; preserve ambiguous outcomes for recovery |
| [#48](https://github.com/feder-positronics/loop-zero/issues/48), [#32](https://github.com/feder-positronics/loop-zero/issues/32) | Implemented renewal is not the same as deployed renewal; account capacity and ownership remain operational dependencies | Verify timer/upload acceptance and account ownership, then introduce declared accounts and typed capacity evidence |
| [#57](https://github.com/feder-positronics/loop-zero/issues/57), [#18](https://github.com/feder-positronics/loop-zero/issues/18) | Durable reservations or prepublication findings outlive an interrupted caller | Recovery from authenticated existing state at each external side-effect boundary |
| [#39](https://github.com/feder-positronics/loop-zero/issues/39) | Shipped adoption remains open for a future measurement, inviting duplicate implementation | Retain its measurement date and original baseline; exclude it from immediate implementation selection |
| [#35](https://github.com/feder-positronics/loop-zero/issues/35) | A paid account decision appears alongside code defects | Keep the explicit owner dependency visible; do not route it to a repair loop |

These are observations from issue bodies and their latest comments, not claims
that every proposed mechanism is absent. In particular, #39's package work and
consumer adoption shipped; #48's setup-token handling, renewal wrapper, and
managed-interpreter fix shipped. Their remaining acceptance is different work.

## Chosen direction

Make the existing delivery path declare its prerequisites and recovery action.
Do not add another scheduler, ledger, dashboard, or generic retry framework.

1. **Make ordinary validation independent of host history.** Unit/reference
   tests use checked-in inputs, a recorded source revision, and fixed filesystem
   observations. Preserve independent expected output rather than recomputing
   it with the implementation under test. Live consumer comparisons require an
   explicit checkout and revision and report missing prerequisites as unavailable.
   A pristine CI checkout must execute the reference assertions.
2. **Check the complete delivery path before expensive work.** The existing
   intake/readiness surfaces should report the required runtime/model family,
   credential lifetime/capacity evidence, validation dependencies, and publication
   prerequisites. Include the independent reviewer requirements, not merely the
   builder's ability to start. A preflight is evidence with a timestamp, not a
   reservation or a guarantee: revalidate at use, and never probe by spending a
   paid turn or exposing credential material.
3. **Treat every external mutation as a recoverable transaction.** Distinguish
   not attempted, attempted with uncertain outcome, durably completed, and
   reconciled from authoritative evidence. Define the crash windows and tests
   before adding retries. For credentials, saving a valid rotation and deciding
   whether its access token can cover an executor are separate decisions. For
   review slots, retained reservations cannot be freed by time or by a new run
   identifier; reconcile process and authenticated lifecycle evidence.
4. **Distinguish implementation from operational and timed acceptance.** Use
   the existing issue and latest acceptance evidence as authority. Selection
   should derive whether the next action is code, reconciliation, an operator
   action, or a measurement after a named date. A shipped item awaiting a week
   of observations does not call for another implementation. A blocked item
   names its dependency and recovery action once; unrelated ready work continues.
   Stop the campaign only for a shared failure affecting safe selection/delivery.
5. **Measure recurrence and time blocked.** Reuse issue/PR timestamps and existing
   dispatch/review records. Compare blocker class, elapsed blocked time,
   repeated failures without changed evidence, and repeated verification of
   unchanged content. Preserve the #39 baseline and its agreed observation
   window. Raw closed-issue count is insufficient evidence of improvement.

## Delivery boundaries and order

First remove the deterministic validation dependencies (#61, #43, #21). Then
address the host rotation loss (#63), with #64's identity contract kept explicit,
followed by the verified operational acceptance in #48. Work on #32 should reuse
that stable single-account boundary; adding more accounts before recovery works
would multiply the same failure. Continue #57/#18 from their authenticated
existing history, preserving already-shipped mechanisms and review evidence.
#39 remains a dated measurement rather than fresh admission architecture. Its
window is the first week of adopted dispatches; derive the due date from the
first authenticated adopted dispatch (not this document date), retaining the
2026-09-14 baseline named in the issue. If that start is unavailable, collect
that evidence before claiming the measurement is due.

This is priority guidance, not a serial dependency chain. An owner-dependent
#48 acceptance does not delay independent #57/#18 work; only actual shared
prerequisites constrain ordering.

All authority changes retain the [authority review bar](authority-review-bar.md):
failing native-record probes, independent reviewers from another model family,
and no weakened soundness gate. Checking reviewer readiness earlier is a
scheduling improvement, not a waiver when reviewers are unavailable.

The strongest alternative is more automatic retry and fallback. It could reduce
transient waits quickly, but a repeated refresh can spend a rotated token and a
repeated launch can spend a review slot twice. Retry is justified only after its
owner can distinguish a safe retry from an unknown side effect. Another broad
rewrite would also discard working #39 adoption and compound integration risk.

## Acceptance and rollback

- Reference tests run in clean CI with no consumer checkout and fail on changed
  rendered bytes or credential-binding argv.
- Cleanup tests assert no leaked reader while dedicated timeout tests preserve
  the product's timeout bound; raising the test grace alone does not loosen it.
- Credential probes cover insufficient lifetime, host install failure, timeout
  after rotation, concurrent source change, and restart. Insufficient snapshots
  never escape; a known-spent token is never silently retried.
- Recovery probes interrupt immediately before and after each durable write or
  external launch. Re-entry recovers the original identity and authorizes at
  most one launch; uncertain evidence stays blocked with one concrete action.
- Intake distinguishes a closed source implementation from unmet deployment or
  dated measurement acceptance, using fresh issue/PR evidence.

Ship these as independently reviewed changes under the existing issues. Revert
code changes independently when necessary, but never roll back credential
rotation or reset authenticated reservation/history as a recovery technique.
This document itself changes no runtime or policy enforcement.

## Owner-dependent questions

The dedicated CI account and deployment acceptance remain with #48; the paid
Cursor choice remains with #35. No account purchase, billing-mode change,
credential mutation, timer installation, or extra model spending is authorized
by this assessment. Existing issue discussions own those decisions.
