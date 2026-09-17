# Workflow migration: executable simplification plan

Status: selected implementation proposal, 2026-09-17. Marcin authorized document
refinement followed by implementation in the orchestration request. This plan
specifies that work; it does not confer merge authority or change running-task
contracts before the reviewed implementation is adopted. Shared owner:
[loop-zero #105](https://github.com/feder-positronics/loop-zero/issues/105).
Consumer owner: [IntelFlo #4466](https://github.com/feder-positronics/intelflo/issues/4466).
Rationale and alternatives remain in the
[exploration](2026-09-17-workflow-migration-simplification.md).

## Selected outcome

One procedure governs genuinely new tasks: owned worktree, draft PR, implementation
and applicable validation, authenticated independent review, verified repairs,
mark ready, exact-source final CI, authorized merge, remote verification and
owned-resource cleanup. These are ordered dependencies, not persisted phases.
Risk changes required evidence and reviewer coverage, never the procedure.

Use `loop-zero-v2` as an immutable contract identity for new admission. Preserve
every existing `intelflo-v1` and `loop-zero-v1` run under its original authority,
including frozen and interrupted runs. This is versioned compatibility, not a
temporary feature flag. Missing historical contract fields remain historical;
an old run cannot gain v2 authority through a caller argument or rewritten row.
Use one contract-admission selector at the boundary, not scattered caller-owned
version conditionals. This migration's already-started shared run
`sr_88c47664d4e14df18e530d7b7487066e` itself keeps its original contract.

The PR body owns objective, acceptance and the delivery evidence summary.
Authenticated primary/delta result receipts own findings and their dispositions;
PR review threads project those facts for people. Thread text, resolution clicks
and a digest without authenticated provenance cannot confer authority. Existing
review budget, source-equivalence, credential containment and merge permissions
survive. No new lifecycle, database, scheduler or readiness-state engine is added.

## Shared interfaces and consumer boundary

Names below describe implementation seams, not a commitment to add one module
or command per name. Extend existing functions where doing so is smaller.

| Interface | Inputs and output | Required behavior |
| --- | --- | --- |
| Draft admission | Owned source checkout, repository, branch, base, clean publishable head, objective/acceptance body; returns verified repository/PR/current head | Verify local ownership and remote source, create or adopt one draft, reject ambiguous/foreign matches, reconcile unknown remote outcomes before retry. Does not require review, closed findings, ready evidence or final CI. |
| Formal-review registration | Existing authenticated registration plus verified repository and PR identity, exact source/base and existing review obligations | Host verifies live PR identity before signing registration. Refuse v2 formal review without it. Bind PR to the attempt, not to a subsequent publication callback. |
| Review result | Existing signed terminal/result artifact, primary findings or delta dispositions, exact registered source | Keep existing authentication, source coverage and atomic budget admission. Derive material finding identities from the authenticated result and existing stable finding-ID primitive. |
| Readiness verification | Current PR/source/base, admitted primary/delta receipts, applicable validation and required coverage | Pure fail-closed verification; no new writable readiness record. Returns pass/fail and diagnostics. Recompute at source-sensitive boundaries. |
| PR projection | Authenticated result/disposition identities plus exact repository/PR | Idempotent review-thread projection. Existing matching authenticated content may be reused; unrelated or edited text cannot substitute. Projection failure leaves a recoverable unpublished result and blocks readiness, not a new review attempt. |
| Mark ready and final CI | Verified current candidate and PR | Mark ready only after local validation/review readiness. Then obtain consumer-required final CI against exact head/base and existing merge authorization. Draft creation never means ready. |

IntelFlo `scripts/util/pr_publish.py::main` currently assembles review/ledger
requirements before `PublicationRequest`; draft admission must occur before that
assembly. Its `publication_request` installs the provisional-binding callback.
Shared `delivery/publish.py` is a library; changing its REST `draft` boolean alone
does not change the consumer path. Draft adoption must retain live-source checks
while excluding `require_resolved_review_threads` and ready-body validation.

## Finding and disposition contract

Reuse the existing signed review terminal/result envelope. The current runner
result has findings and review sections; it does not prove disposition merely
because a later review reports no new findings. Extend delta result validation
and its authenticated task contract minimally:

- The task names the authenticated primary result digest and the complete set
  of its critical/important finding IDs still requiring disposition.
- The delta result supplies exactly one disposition per required ID:
  `{finding_id, outcome, rationale}`, where outcome is `repaired`, `rejected`
  or `unresolved`. `rejected` requires the independent reviewer to explain why
  the original claim does not hold against the reviewed acceptance/source.
- Missing, duplicate or unknown IDs, a changed primary digest, unsupported
  outcomes, or an unsigned/foreign-source result fail closed. All newly raised
  critical/important delta findings remain open. Suggestions never enter the
  blocking set or durable debt automatically.
- `repaired` requires admitted delta review of the changed source plus affected
  validation; it is not a caller assertion. Mechanical evidence carry still
  requires kernel-proven equivalence. A clean delta alone never closes the
  primary review's findings.
- Initially, waiver admission is unavailable unless an existing authenticated
  merge-authority route proves the exact repository, PR, source, finding and
  rationale. Legacy authority-name strings and GitHub thread clicks are
  insufficient. Unsupported waiver requests remain blocked with that reason;
  do not invent an unsigned owner-approval mechanism.

Use `review/authority.py` authentication and delta admission, existing signed
registration storage, and `kernel/authority_projection.py` generation/slot
projections. Do not weaken one primary plus one bounded delta per generation.
An exhausted budget leaves an adoptable diff and explicit remaining scope for
the owner; no identity reset or clean-PR trick creates another slot.

## Ordered implementation units

These units are a finite implementation/delegation plan, not another task ledger.

| Unit | Owning changes | Acceptance and dependency |
| --- | --- | --- |
| S1: immutable admission and draft | `kernel/run_identity.py`, `delivery/publish.py`; consumer publisher early entry | Existing runs preserve identity; genuine new starts select v2; open-finding/no-review drafts create and adopt safely. Consumer integration is necessary before claiming cycle repair. |
| S2: PR-bound authenticated review | Existing registration/authority validation and `review/evidence.py` intake routing | Verify PR before signed registration; v2 formal review writes no provisional binding. Reject forged/foreign PR identity and changed source. Depends on S1 draft primitive. |
| S3: authenticated dispositions and projection | Result schema/normalization, `review/authority.py`, narrow PR projection/readiness adapter | Real primary blocker, substantive repair, admitted delta dispositions and checks yield readiness. Missing dispositions, forged receipts, new blockers and exhausted slots fail closed. Depends on S2. |
| C1: consumer convergence | IntelFlo procedure, wrappers, canonical rules/skills and generated mirrors | All new ordinary/security-sensitive tasks use v2 with appropriate checks; remove pilot/standard fork and independent manual capture/lease/closure/publication prerequisites. Depends on S1–S3 reviewed package. |
| C2: CI and mark-ready | Consumer CI triggers, protected required-check aggregation and closeout adapter | Mark ready precedes final CI. Missing/skipped required jobs and head/base drift block. Keep existing custom final gates until the replacement is trigger-complete and actually required by the live ruleset. |
| S4/C3: retirement and adoption | Remove final callers and obsolete tests, pin/snapshot/install, historical-only entry | Demonstrate net executable deletion, preserved historical re-entry, installed consumer complete path and immutable artifact selection. Depends on replacement evidence and live historical inventory. |

S1 and S2 may be developed as one bounded package change if that avoids a
temporarily unusable interface. S3 can prepare schemas/probes independently but
cannot grant readiness before integration. Consumer docs and CI trigger analysis
can proceed alongside shared implementation. Assign completed agents to the next
ready unit; do not wait for unrelated historical settlement or product evaluation
to perform independent work.

## Removal and preservation map

| Current responsibility | v2 destination | Historical handling |
| --- | --- | --- |
| `publish.py::validate_publication_request` mixes creation and readiness | Separate minimal draft source admission from readiness verification | Original ready-publication behavior remains reachable only for original contracts |
| `publish.py` post-creation `bind_provisional_findings` callback and consumer callback assembly | No v2 invocation; PR registered before review | Retain for evidenced historical pre-PR review recovery |
| `findings.py::authenticated_delivery_run_pr` derives identity from delivery terminal/binding | v2 registration carries authenticated PR | Preserve original derivation for old records |
| `evidence.py::append_finding_records`, `authority.py::fold_in_governed_review_findings` manual ledger intake | Signed result plus PR projection; no independent v2 capture/closure requirement | Preserve old capture/provenance obligations on original runs |
| Finding leases, severity corrections, terminal handoff/closure operations | Removed from v2 workflow; material defect disposition remains signed review evidence | Historical readers/writers only until identified owners settle |
| `_publish_threads.py` resolution-click readiness | Strict complete reader retained; signed disposition determines material finding status | Original thread policy retained where original contract requires it |
| Consumer phase/checkpoint/partition, duplicate finding/publication bodies, separate pilot | One new-task procedure and one PR evidence summary | Load historical instructions only for historical re-entry |
| `delivery/closeout.py` privileged pinned-toolchain bootstrap | Preserve necessary source/authority boundary and remote verification | Do not delete as ceremony merely because it is large |

Whole shared modules are not currently proved dead. Moving them into a legacy
directory earns no deletion credit. Delete obsolete responsibilities and their
last callers/tests together. Regenerate adapters from canonical source; never
patch the vendor snapshot to simulate completion.

There is also a concrete deletion unit independent of draining all historical
runs: IntelFlo's five internal publisher facades `pr_publish_findings.py`,
`pr_publish_obligations.py`, `pr_publish_paths.py`, `pr_publish_remote.py` and
`pr_publish_threads.py` total 4,165 physical lines in the consumer census.
The caller audit identifies consumer-owned generation in IntelFlo
`scripts/make/loopzero_shims.py`; no shared production generator change is needed.
The same census records 2,951 noncomment/non-docstring lines and 1,961 AST
statements. Verify dynamic/historical callers, then remove consumer generation
entries, facade files and obsolete consumer preservation assertions together.
Preserve the shared historical fixture and corresponding shared
`delivery/_publish_*` implementations, which the public publisher still uses.
This is executable code deletion, not relocation or deletion of historical state.

Before retirement, record in the existing coordinating issue the exact live
historical run IDs, repository/branch, immutable contract, owner, terminal state,
remaining obligation and adapter used. Inventory all relevant canonical state
roots; an empty worktree-local directory does not prove no historical runs.
No run IDs were authenticated by this document's read-only source inspection.
An unknown inventory blocks deletion of the affected compatibility path, not
independent replacement implementation. Retire each adapter when its last run
is verifiably settled or explicitly retired by its authority; never erase or
relabel the original history.

## Runtime installation and state ownership

Select an immutable installed artifact by reviewed package revision plus matching
consumer policy/snapshot identity. A task records and resumes that exact tuple.
Build/validate in a private temporary installation and publish it atomically to
the revision-keyed directory; never mutate a directory selected by another live
task. Do not replace a common runtime during branch switch or re-entry.

Artifact immutability does not by itself protect shared authority state. Keep
the existing serialized authority writer and locks, with monotonic schema and
reader/writer compatibility checks before mutation. An older pinned client must
fail before writing unsupported state, not downgrade or reinterpret it. If a
client cannot safely read current state, preserve its original task and report
the compatibility blocker. No silent receipt migration or epoch reset.

## Evidence and deletion budget

Inspection baseline is shared `0123a64f`; IntelFlo baseline and final adoption
SHA belong in #4466's implementation PR. Shared physical-line census:

| File under `src/loopzero/` | Lines |
| --- | ---: |
| `review/findings.py` | 2,265 |
| `review/provisional_findings.py` | 1,076 |
| `review/ordinary_findings.py` | 465 |
| `review/evidence.py` | 1,350 |
| `review/authority.py` | 1,728 |
| `delivery/publish.py` | 1,327 |
| `delivery/_publish_threads.py` | 200 |
| Total inspected | 8,411 |

These are physical lines, not an executable deletion claim. Before implementation,
capture one reproducible baseline of executable workflow code, mandatory operator
actions, manually maintained record types and fresh-task entry points across both
repositories. Count upstream package code once; report consumer generated/vendor
copies separately. Use the same paths/classification and counter before/after,
including newly added code and replacement tests. Put results in existing PRs;
no standing collector or new report service is required.

Acceptance requires net removal of executable workflow code across the package
and consumer, one fresh-task procedure, no independent manual finding disposition
record, no provisional binding in v2 formal review and fewer operator actions.
Do not claim full simplification when only draft admission has shipped. Historical
compatibility may remain only with identified live owners and finite retirement
criteria; unknown leftovers remain explicit unfinished scope.

## Validation and independent review

Use Python 3.14 and the credential-free, read-only-source Bubblewrap validation
boundary in [package CI](../../.github/workflows/checks.yml). Run candidate tests
only inside that boundary or its verified equivalent. Start with owning
publication, findings, provisional-recovery and evidence tests under
`tests/unit/delivery/` and `tests/unit/review/`; add probes for the selected new
contract and dispositions, then run touched authority/budget lanes and the package
suite. Retain meaningful historical negative tests; delete obsolete tests only
with their final responsibility. Run installed-wheel/sdist and consumer-launcher
tests, not only import/mocked unit tests.

Required complete-path scenarios: draft with no review/open blocker; genuine
blocker repair; forged/edited disposition; missing/duplicate/unknown IDs; new delta
blocker; changed head/base; missing/skipped required CI; ambiguous draft creation;
interruption before/after projection, remote merge and cleanup; old pinned writer
rejection; historical re-entry with tracked and untracked work preserved. Reconcile
remote success before retry; failed cleanup never changes a verified merge into
an unmerged result or pretends cleanup passed.

Apply the [authority review bar](authority-review-bar.md) and its cross-family
review requirements to touched trust boundaries. Exact-source independent review
and applicable security review remain required. Existing #111 strict thread
pagination and #116 merged kept-work settlement are already repaired on this
baseline; regression-test them rather than reopening their implementation scope.

The authorized two bounded IntelFlo product deliveries evaluate the adopted
procedure. They are not workflow-only test PRs, and they do not establish the
separate second-product-consumer portability claim. Do not invent another pilot
or report either evaluation complete without its actual evidence.

## Selected amendments and completion authority

These refinements explicitly propose superseding the relevant prospective parts
of the [earlier decision record](2026-09-11-decision-record.md) on v2 adoption:

- D3/D17: finish cutover B through evidenced deletion; keep the existing size
  ceiling, without treating it as a target to fill.
- D4/D21: preserve developer capability and original-run behavior, not every
  legacy command for every new task. Removed new-task commands have a documented
  surviving operation; historical commands remain only where required. This
  amends command-preservation expectations without weakening safety acceptance.
- D18: no new SQLite/state engine is needed for this simplification. Reuse
  existing authority/ownership state and derive delivery status from source,
  authenticated evidence and remote facts.
- D29 and D22/D23 remain: content-bound budget/authenticity, credential isolation
  and cross-family review are preserved.

The authority review bar's soundness/liveness distinction remains useful.
Material liveness problems outside this change become bounded follow-ups rather
than perpetual blanket blockers. However, draft-first blocker repair, recovery
and removal are this change's required acceptance: inability to perform them
blocks claiming completion even if the failure is conservative. Neither
"liveness" nor an exhausted review slot waives required acceptance or permits an
unearned verdict. Resolving that conflict does not authorize more review slots.
Specifically amend Method 4: liveness is an impact class, not an automatic
severity downgrade; a critical/important broken documented path still blocks
unless an authenticated permitted waiver exists. Amend Method 2: a rare path may
deny carry and request only an already-admitted review; if no slot is available,
preserve work and obtain an owner scope decision, never mint an extra primary.

Implementation is now authorized by the owner; no further generic design-permission
pause is required. Review/merge authorization and live consumer ruleset authority
remain as already established for their actual actions. Final reporting must
separate shipped implementation, verified deletions, historical settlement and
product/portability evidence; unresolved required work remains unfinished.
