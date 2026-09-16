# Prospective independent-review outage recovery

Issue #80 adds one explicit owner-authorized replacement after both ordinary
attempts for the same review obligation ended in authenticated empty provider
failure. The owner accepted prospective-only recovery on 2026-09-16: legacy or
unknown output evidence remains blocked. Missing output artifacts, zero usage,
released slots, diagnostic strings and successful credential readiness are not
proof that no model output occurred.

## Authority and bounds

The trusted coordinator records a distinct, signed authorization. It binds the
repository, exact source, run, immutable review task, work unit, content lineage,
generation, family and slot; both failed receipts and settlements; the executor
whose work is being reviewed; and one named replacement route. A new immutable
review contract references the authenticated executor terminal. Its actual
actor/model identity and resulting source establish what the review covers.
Unknown identity fails closed. Independence is not a blanket cross-vendor rule.

Ordinary routing and slot retry limits remain two attempts. The authorization
permits exactly one exceptional reservation in the same obligation, without
removing or discounting the two failed attempts. The failed task keeps its ID;
the new attempt advances its attempt index and work-unit attempt number. Both
successor counters are bound in the grant. The reservation's idempotency key
binds the authorization digest. Durably recording that reservation spends the
authorization, even if the coordinator is interrupted before launch. Idempotent
replay never returns another launch permission. This operation does not add
crash refunds or change #57 recovery policy.

No second grant can renew the same obligation through another task name, work
unit, route, replacement failure, or ledger compaction. Compaction retains the
signed grant, referenced executor and failed-attempt receipts, their authority
registrations, all original reservations/settlements and the replacement's
consumption and terminal evidence. Partial review, findings, semantic/model
failure, active work, source drift and unavailable identities are ineligible.

## Consumer boundary

The consumer must adopt both the reviewed package and its admission contract.
Changing the shared slot function alone does not change IntelFlo's independent
retry-policy implementation. Consumers must expose an explicit owner command
through their existing coordinator signing boundary; a caller flag or a JSON
object claiming owner approval has no authority.

The owner command validates current source and authenticated history under
worktree ownership and the authority-ledger lock, then signs, appends and fsyncs
the derived grant. At admission, the consumer reloads authenticated history and
validates that exact grant before using the exceptional path. All ordinary
acceptance, evidence, source, budget and circuit gates still apply. Failed
provider circuits remain active. A replacement's own circuit or budget refusal
cannot select another route or restore the consumed grant.

The consumer durably records one recovery reservation, then atomically checks
and registers its unique start before launch. Actual runtime identity must
match the authorized replacement before its terminal can supply a verdict.
Package APIs retain their authenticated record views; no conversion to a plain
list or synthetic authentication attribute is admissible. Trusted storage
callbacks must preserve authentication and propagate partial-write errors.

Runtime output observation is monotonic and typed. The trusted runtime adapter
must set it before output normalization, discarding or budget enforcement, and
the dispatcher must include it in its signed terminal. Observed model content
wins over a contradictory empty marker. Legacy/untyped transports stay unknown.
Sanitized provider diagnostics remain explanatory, never eligibility authority.

The source-owned adapter boundary is:

- Add `reviewed_executor_terminal_ref` to the immutable review contract before
  the first attempt; record the runtime's typed `model_output_seen` in each
  signed terminal. Do not backfill either fact into historical receipts.
- Under the worktree lease and ledger lock, use `prepare_outage_recovery`, then
  sign, append and fsync its returned record through the existing coordinator.
  An unsigned returned dictionary grants nothing.
- Call `admit_outage_review` with the exact grant digest, the original task
  contract and task ID, and successor attempt counters. Its mandatory trusted
  `admit_execution` callback applies the existing capability, circuit and budget
  gates. This is the sole exceptional routing path; ordinary retry validation
  remains unchanged and rejects a third ordinary attempt.
- Before launching, reload under the same locks and call
  `validate_outage_recovery_start`; reapply the normal execution gates and
  durably register exactly one start. The start and terminal retain the grant
  digest as `outage_authorization_sha256`, reservation, route and counters.
- A coordinator verdict for this task must include `terminal_ref`, the digest
  of the exact replacement terminal. Reusing the task ID cannot retroactively
  turn an earlier failed attempt into an accepted review.

The grant cannot override normal content, coverage or scope admission. If those
rules require a different obligation, recovery remains blocked. The source API
does not itself deploy an owner command or change any consumer pin.

IntelFlo #4434 source adoption and the PDF export retry retain their existing
owner. They are not tests or launch authority for this new recovery operation.
