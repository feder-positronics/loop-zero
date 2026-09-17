# Exploration: Finish the workflow migration through simplification

Status: exploration refined into an implementation proposal, 2026-09-17. Owner: Marcin. Tracking:
[loop-zero #105](https://github.com/feder-positronics/loop-zero/issues/105) and
[IntelFlo #4466](https://github.com/feder-positronics/intelflo/issues/4466).
The owner subsequently authorized refinement followed by implementation. The
[executable plan](2026-09-17-workflow-migration-implementation.md) selects the
interfaces, amendments and deletion boundaries. Adoption of reviewed code changes
the active [core contract](../../core/CONTRACT.md); this document alone does not
rewrite existing runs or grant merge authority.

## Outcome and scope

Finish the migration by deleting overlapping workflow obligations. A new task
should have one delivery procedure, one owner, one review disposition and one
readiness verdict. Risk changes the evidence required, not the lifecycle used.
Moving an old mechanism from IntelFlo into this package is not simplification.

The founder-builder needs useful changes delivered without managing internal
records. The independent reviewer needs an immutable candidate and acceptance
criteria. The merge owner needs authentic review, applicable checks and an
unambiguous unresolved-defect view. A successor needs preserved work and a safe
next action after interruption. These are development-tooling actors; no
IntelFlo product UI, public API or scientific workflow changes are proposed.

This exploration owns the shared design under #105. IntelFlo owns its consumer
policy, branch protection, adoption and historical settlement under #4466.
The companion file there is
`docs/design/explorations/2026-09-17-workflow-migration-simplification.md`.
Issue links are the cross-repository coordination entry points until docs ship.

## Verified current state

Inspection baseline: loop-zero `0123a64f` and IntelFlo `f2d64cea9` on 2026-09-17.
IntelFlo pins `9c21040df343850dfadf63397d8b5bbf46ba35de`; source main and the
installed consumer must not be assumed identical.

- The [architecture](2026-09-11-executable-core-architecture.md) deliberately
  split moving mechanisms (A) from rebuilding/deleting orchestration (B).
  D3/D17 in the [decision record](2026-09-11-decision-record.md) preserve that
  distinction. A moved package and a changed primary-contract label do not
  establish completion of B.
- The core says findings belong to a PR and deterministic checks replace
  closure bookkeeping. It also specifies ledger-owned review slots, generations,
  claim receipts and authority-bound reuse. Those are real implementation
  constraints, not merely stale words that can safely be deleted first.
- IntelFlo's delivery guide retains a separate two-delivery low-risk product
  pilot and a custom publisher/closeout route for other new tasks. Its original
  cutover plans and legacy rules are still discoverable alongside them.
- `validate_publication_request` in
  [publish.py](../../src/loopzero/delivery/publish.py) rejects
  `open_finding_ids` before remote creation; `bind_provisional_findings` is
  invoked after the PR has been created/adopted and revalidated.
  [findings.py](../../src/loopzero/review/findings.py)'s
  `authenticated_delivery_run_pr` rejects a missing unique authenticated
  binding; [evidence.py](../../src/loopzero/review/evidence.py) uses it for
  delivery-run finding evidence. This verifies the dependency edges behind the
  reported closure/publication cycle. It does not establish that every draft
  or historical recovery route fails; complete-path reproduction is required.
- [Preventing delivery blockages](2026-09-16-preventing-delivery-blockages.md)
  addresses recovery and admission; #105 also identifies mutable toolchains and
  complete-path proof. The present exploration adds a removal contract rather
  than another retry framework.

## Alternatives

| Option | Benefit | Cost and uncertainty |
| --- | --- | --- |
| A. Repair the current binding order and retain the rest | Smallest immediate incident fix; preserves current authority paths | Leaves pilot/standard divergence and record administration intact; no evidence of migration completion |
| B. One procedure with draft-first review and a small verification boundary | Removes provisional ownership from new review; reuses Git/GitHub and existing isolation/checks | Requires proof of authentic review disposition and CI enforcement before old gates can be removed |
| C. Rebuild orchestration as a new general state engine | Could unify retries and external effects | Adds a new migration/store and delays deletion; the earlier architecture's complexity ceiling is not a target to fill |

Recommend B. A remains valid for independently owned urgent repairs but cannot
close this migration. C is the strongest general alternative; pursue it only
if a bounded complete-path prototype proves an essential obligation cannot be
met with existing primitives and it yields net deletion. No new database,
scheduler, dashboard, ledger or permanent compatibility mode is proposed.

## Proposed minimal contract

1. Reserve one owned worktree; record objective, acceptance and base. Freeze
   the approved toolchain identity for this delivery. Create a draft PR once a
   publishable branch exists, before formal delivery review. Local exploratory
   work can precede the draft; a network outage preserves that work without
   conferring readiness.
2. Implement and run applicable checks under credential and Git-metadata
   containment. Commit the candidate and obtain authenticated independent
   review of that exact source. Preserve required security coverage and the
   existing bounded review budget; this proposal grants no extra paid attempts.
3. Record material findings and dispositions in the PR review. A blocking
   finding remains blocking until a verified repair or an explicit authorized
   waiver. A UI thread-resolution click alone is not proof. Substantive repair
   needs the permitted delta review and affected checks; mechanical evidence
   carry requires proven equivalence.
4. Compute readiness from current source, authentic review/dispositions and
   applicable local checks, then mark the PR ready before obtaining final CI.
   Merge only with current base compatibility, exact-head CI
   and existing merge authorization. Creation or updating of a draft is never
   a readiness verdict. A changed head invalidates dependent evidence.
5. Verify remote merge, then clean up only owned resources. Derive status from
   Git, GitHub and authentic receipts. Record a failed cleanup as unsettled
   cleanup, without changing a verified merge into an unmerged delivery or
   inventing a successful cleanup result.

| Fact | Authoritative home | Supporting mechanism |
| --- | --- | --- |
| Source and base identity | Git commits | Recompute identity at review/check/merge boundaries |
| Objective, acceptance, delivery summary | One PR body evidence block | Temporary task text moves here when draft exists |
| Review findings and resolutions | Authenticated primary/delta result receipts | PR threads project signed findings/dispositions; no independent manual closure ledger |
| Validation results | Source-bound check artifacts | Host/CI verification; required checks cannot be absent or skipped vacuously |
| Live writer/resource ownership | Existing worktree/resource lease | Only for actual concurrent mutation safety |
| Durable product debt | Consumer KNOWN-GAPS.md | Conscious curation; no automatic finding replay |
| Historical provenance | Preserved original receipts | Read on historical re-entry; no fresh-task checklist |

GitHub text is mutable and is not a replacement for authentication. The
[selected disposition contract](2026-09-17-workflow-migration-implementation.md#finding-and-disposition-contract)
specifies verified reviewer/source identity and signed primary/delta dispositions,
including the exact primary digest and complete material-finding IDs. Reuse
existing authenticated receipts; do not create an independent human-maintained
record for the same fact. Missing authenticity remains a blocker.

## Removal inventory and protected behavior

This is a candidate inventory, not authorization to delete whole modules.
An implementation diff must establish callers and surviving behavior before
removal; shared modules can contain both retained and retired responsibilities.

| Surface | Proposed disposition | Proof required before removal |
| --- | --- | --- |
| IntelFlo pilot and standard new-task choreography | Converge into one procedure | Fresh-entry scenarios for ordinary and security-sensitive changes |
| New-task phase/checkpoint/partition/run-ledger rituals | Remove independent obligations; keep only actual ownership/provenance | Interrupted work survives; concurrent writers remain excluded |
| `review/provisional_findings.py` publication bindings | No use for new formal review; retain only evidenced historical use until drained | Draft → blocking finding → repair → merge succeeds with no provisional binding |
| Duplicate finding leases, closures and PR projections | Remove new-task duplication; preserve substantive defect disposition | Concurrent review cannot lose or falsely resolve a material finding |
| `delivery/publish.py` and IntelFlo publisher overlays | Reduce to necessary checks/effects or retire replaced parts | Draft creation is independent of readiness; foreign source and stale evidence rejected |
| Custom merge/final-CI gates | Retire only after equivalent enforced replacement | Missing/skipped/failed required jobs cannot merge; exact head/base checked |
| Closeout/settlement continuations | Retain necessary idempotent remote verification; remove bookkeeping prerequisites to verified merge | Crash after remote success cannot cause duplicate merge or false cleanup pass |
| Review generation/slot/claim machinery | Retain soundness controls until bounded reuse can be proven with fewer mechanisms | Budget, provenance, source equivalence and security coverage adversarial tests |
| Generated facades and tests | Delete with their final callers; regenerate from canonical source | Installed wheel plus consumer launcher works; no shadow loader or stale imports |
| Historical contract adapters | Historical-only, with finite owner inventory and retirement criteria | Old run re-entry preserved; no fresh run admitted; no data deletion or silent epoch reset |

The shared package owns generic mechanism changes. IntelFlo owns consumer
rules, hooks, wrappers, CI/rulesets and pin adoption. Change package source
upstream and adopt a reviewed revision; never patch an installed package or
consumer vendor snapshot to simulate completion.

## Proof, complexity budget and rollback

Before promotion, capture the existing behavior and count mandatory operator
steps, manually maintained record types, fresh-task contract entry points and
executable lines across both repositories. Record one compact before/after
table in the eventual implementation PR; do not create a standing collector.
Target: one new-task procedure, no duplicate manually maintained disposition,
no provisional binding for new formal review, fewer required operator actions,
and net removal of executable workflow code across both repositories. Moving
files or replacing code with more prompt instructions earns no deletion credit.
Existing architecture budgets remain constraints until explicitly amended.

Acceptance scenarios must exercise the installed package through the consumer
launcher, not only mocked package internals:

- Fresh ordinary and security-sensitive tasks load the same procedure and the
  appropriate evidence requirements; draft with an open finding is allowed,
  readiness/merge with an unresolved blocker is refused.
- A real blocking review finding is repaired, reviewed within the existing
  budget, validated and merged without publication-binding workaround.
- Source/base drift, forged reviewer evidence, foreign worktrees, missing
  required checks and skipped required jobs fail closed.
- Interrupt before/after draft creation, review deposition, remote merge and
  cleanup. Re-entry reconciles actual remote state and preserves tracked and
  untracked work; unknown side effects do not justify blind retries.
- An old pinned consumer cannot downgrade shared runtime/schema state. Freeze
  source/toolchain tuples and define a single owner for shared runtime mutation;
  prefer task-local installed artifacts where practical.
- Historical run re-entry retains original authority, failures and remaining
  obligations. New tasks cannot enter historical compatibility.

Use the already-authorized IntelFlo two bounded product fixes as evidence where
its exact sources and procedure qualify. #105's original three-delivery pilot
was a proposal, not a separate authorization or a reason to launch a second
campaign. Do not count workflow-only changes as product proof. Cross-project
portability still needs the separately chosen second product consumer; working
on the framework and IntelFlo docs does not satisfy it.

Record elapsed/blocked time, repair rounds, owner interventions and observed
regressions in existing PRs, with costs marked unknown when unavailable. A
small sample diagnoses friction; it cannot prove a long-run defect rate.

Prepare in isolated branches, prove replacements before removing enforcement,
then adopt one reviewed package revision and its matching snapshot. Roll back
code/pins only where state compatibility is demonstrated. Otherwise fence new
admission and preserve work while repairing forward. Never revert credential
rotation, erase receipts, relabel old runs or reset authority state as rollback.

## Decisions and promotion gate

The owner authorized final document refinement and full implementation. The
[selected implementation plan](2026-09-17-workflow-migration-implementation.md)
specifies `loop-zero-v2` for genuinely new tasks, draft-first admission, signed
primary/delta disposition authority, consumer convergence and concrete removal
acceptance. Existing `intelflo-v1` and `loop-zero-v1` runs retain their original
contracts. A resolved PR thread is a projection, not proof of repair or waiver.

#105 owns shared interfaces and evidence; #4466 owns consumer procedure,
ruleset/CI replacement and adoption. Historical owners must identify live callers
before their compatibility is removed. Use bounded implementation PRs and the
existing coordinating issues, retaining repair/measurement owners. Implementation
authorization is established; exact-source review, required acceptance and merge
authority remain required. Product evaluation and second-consumer portability
are separate evidence, not reasons to invent another workflow pilot.
