# D29 implementation spec: verdicts as content facts (reconciled 2026-09-14)

Reconciled from two independent designs (Claude Fable 5.1: `review-design-fable.md`; OpenAI gpt-6-astra: `review-design-astra.md`). They agree on structure; where they differ the safer rule is taken and marked.

## 1. Content generations (kernel)

`kernel/review_state.py::resolve_generation`. A generation starts on the first candidate or on a `PatchIdentityV1` change without a kernel proof of equivalence. Same generation: `prove_patch_equivalence` succeeds (patch id, ancestor bases, exact replay, resulting tree), or `prove_format_only` proves identity in its supported scope. Anything else, including "mechanical" fixes without a proof, starts a new generation (Astra, safer). Record `ReviewGenerationV1 {repository_binding, lineage_id, generation_id, predecessor_id, patch_identity, tree, required_sections, policy_digest, delta_from_tree, delta_sha256, changed_paths, dependency_paths, primary_origin_receipt, inherited_coverage, invalidated_sections}`; heads join through append-only `GenerationCarryV1 {generation_id, from_identity, to_identity, proof, sections}`. Lineage derives from authenticated starts and checkpoints, never from caller run ids, branch names or categories; previously seen content is indexed so a revert or rename cannot replenish slots. `review_task_covers_tree` runs per required section before carrying coverage; a security-section rejection of base motion keeps the generation but invalidates security coverage. A repair generation inherits the predecessor's primary receipt as baseline with the changed coverage outstanding; inheritance is not a verdict for the repaired tree.

## 2. Claim-level trust verification (review)

`review/trust_claims.py::{normalize_manifest, claim_id, invalidate_claims, compose_claim_verdict, receipt_covers}`. Each item in the six list fields is a claim `{text, paths}`; `risk_paths` is the required risk inventory, compared mechanically with the trusted classifier, and every required path needs claim coverage. `claim_id = sha256(canonical_json(["trust-claim-v1", section, normalized_text, sorted(unique(paths))]))`; normalization is NFC, CRLF and outer whitespace only (Astra, safer than collapsing internal text); duplicate identities are rejected; paths are canonical repository-relative and traversal-free; changing coverage changes identity. A claim with no paths depends on the whole repository and is invalidated on any source change (both designs). Invalidate when text or coverage changes, or covered paths intersect the generation's changed paths including additions, deletions, both rename endpoints, mode changes and base motion; carry everything else with its original verifier run id. Manifest-only edits invalidate edited claims; removals are verified as retirements before obligations drop. `TrustClaimTaskV1` is immutable and carries only invalidated claims, the generation diff and their evidence; zero invalidations means no launch. `TrustClaimReceiptV1` binds fresh results per claim with `verifier_run_id`, `verifier_task_id`, `evidence_digest`; a coordinator-authenticated composition references carried and fresh receipts individually; pass requires complete current coverage and every claim passing; inconclusive gives no verdict. Everything except the judgment of a claim is mechanical.

## 3. Review admission (shared dispatcher, calling kernel and review)

`review/admission.py::admit_review(repository, candidate, task, route, policy, idempotency_key) -> Carry(receipts) | Reserved(slot, scoped_task) | Blocked(code, evidence)`. It loads authenticated history, resolves the generation, checks coverage and equivalence, computes the delta scope (`review/scope.py::dependency_closure`, initially changed paths plus security trigger paths, with the import closure a later addition). Zero churn with valid coverage dispatches nothing. An inherited primary admits only a bounded delta over the uncovered diff. `chain.py::enforce_review_budget` is called inside `kernel/review_state.py::reserve_review_slot` with consumed and reserved counts derived from the ledger; slots are keyed by repository, lineage, generation and family (`delivery` or `trust`), at most one primary and one delta each; security is a delivery section; trust claims are batched. Reservation happens under the authority ledger lock with reload and validation, appended durably before launch; one outstanding reservation per family; the lock is never held during inference. Settlement is idempotent and authenticated. Ambiguous crashes retain reservations until process death and absence of a verdict are verified; expiry alone never refunds (Astra, safer than a TTL).

## 4. Typed outcomes and retries (runners, review)

`runners/contract.py::ReviewOutcome` with runner mappings; `review/authority.py::classify_review_outcome`. Release the slot: verified limit, disconnect, budget kill, engine-output failure, operator termination, and `model-result` failures without a valid verdict; trust `inconclusive`. Consume: any completed review with an authenticated pass or fail verdict, `model-result` failure carrying an authenticated verdict, budget exhaustion after a valid result. Unknown or forged outcomes stay unresolved and never free. One retry per `(lineage, generation, family, slot_kind)` obligation across aliases, engines, task ids and equivalent heads; both attempts keep their time and quota charges; a provider failure cannot mint a generation.

## 5. Telemetry

`ReviewLaunchV1` and outcome rows outside the immutable task hash, joined by reservation and attempt ids. Derived reasons: initial, bounded delta, supersession, trust verification, trust delta, security path, owner requested, infrastructure retry; primary plus secondary triggers; labels never grant slots. Record invalidation causes, changed and covered paths, fresh versus carried claim counts, outcome, elapsed time, tokens, quota, API-equivalent USD with `cost_source` and billing mode. `dispatch_stats.py::review_stats` reproduces the 2026-09-14 measurement from the ledger.

## 6. Compatibility

Existing receipts and task hashes stay intact. A legacy whole-manifest pass seeds per-claim references at its source, conservatively repository-wide until coverage is reviewed; no historical rerun. Older binaries refuse to write unsupported ledger authority. From #4415 keep the authenticated lineage derivation, the telemetry separation and the relevant scenarios of its test module, split by package owner; discard per-PR counters and reset choreography.

## 7. Expected effect

Tail PR #4396: trust cost from about 9.6 to 1.8 to 2.8 API-equivalent USD (72 to 81 percent saving); code review from 3.4 to about 1.8. Median PR: no trust calls, about 10 percent from carried repeats. Scenario estimates, not forecasts.

## 8. Delivery order (loop-zero, each with cross-harness review)

1. Claim engine, minimal atomic trust admission, legacy projection. Largest saving.
2. Generation resolution and delivery admission.
3. Typed settlement, retries, archive retention.
4. Statistics, configuration and contract wording, compatibility.
5. One consumer adoption PR: pin bump, facades route to shared admission and statistics.
