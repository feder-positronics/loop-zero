"""Host-coordinator transaction for one native, changed-source delivery delta.

Callbacks are trusted host storage adapters, never candidate/model inputs. The
package derives every edge; signing keys stay in the adapter. This deliberately
refuses later successors and released-delta retries rather than renew capacity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from ..kernel import authority_projection as projection
from ..kernel import authority_store, patch_identity, review_state, worktree_lease
from ..kernel.gitscope import DispatchError, ReviewSnapshot
from . import admission, authority
from ._tree_coverage import accepted_review_patch_identity
from .routing import review_family_for_intent


class LinkRefused(ValueError):
    """Content-free failure of authenticated lineage derivation."""


def _require(condition: object) -> None:
    if not condition:
        raise LinkRefused("generation link evidence is unavailable")


def _tree(repo: Path, commit: str) -> str:
    # Keep process creation inside the existing audited kernel Git boundary.
    return patch_identity._require_stdout(repo, "rev-parse", f"{commit}^{{tree}}")


def _source(
    repo: Path, expected: Mapping[str, object], snapshot: ReviewSnapshot, binding: str
) -> None:
    _require(authority_store.authority_repository_binding(repo) == binding)
    _require(expected.get("version") == 2)
    _require(worktree_lease.source_identity(repo) == dict(expected))
    _require(not projection.worktree_status_paths(repo))
    _require(_tree(repo, snapshot.commit_sha) == snapshot.tree_sha)
    _require(_tree(repo, str(expected.get("head"))) == snapshot.tree_sha)
    identity = snapshot.patch_identity
    _require(isinstance(identity, dict))
    assert identity is not None
    _require(
        identity
        == patch_identity.compute_patch_identity(
            repo,
            base_sha=str(identity.get("base_sha")),
            candidate_sha=str(expected.get("head")),
        )
    )
    _require(identity.get("candidate_tree_sha") == snapshot.tree_sha)


def _derive(repository, worktree, records, task, expected_source, snapshot):
    review_state.assert_authority_ledger_lock_held(repository)
    _require(review_family_for_intent(task.get("review_intent")) == "delivery")
    _require(
        not any(
            key in task
            for key in (
                "review_family",
                "review_generation_id",
                "review_lineage_id",
                "launch_reason",
            )
        )
    )
    binding = authority_store.authority_repository_binding(repository)
    _source(worktree, expected_source, snapshot, binding)
    accepted = authority.accepted_review_terminals(records)
    terminals = [
        terminal
        for terminal in accepted.values()
        if terminal.get("snapshot_sha") == task.get("delta_from_snapshot")
        and terminal.get("run_id") == task.get("run_id")
    ]
    _require(len(terminals) == 1)
    terminal = terminals[0]
    _require(
        terminal.get("run_id")
        and terminal.get("review_intent") == task.get("review_intent")
    )
    old_source = terminal.get("source_identity")
    _require(
        isinstance(old_source, dict)
        and old_source.get("version") == 2
        and old_source.get("ref") == expected_source.get("ref")
    )
    accumulator = getattr(records, "accumulator_head", None)
    if accumulator is not None:
        _require(accumulator.repository_binding == binding)
    generations = projection.generations(records)
    predecessor = generations.get(terminal.get("review_generation_id"))
    _require(predecessor is not None)
    _require(
        any(
            row.get("type") == "review-generation-v1"
            and row.get("generation_id") == predecessor.generation_id
            for row in projection.authenticated_review_state_records(records)
        )
    )
    # Only the original native primary may fund this bounded delivery. Carry
    # endpoints and multi-hop transitions require their own proven policy.
    _require(
        predecessor.predecessor_id is None and predecessor.repository_binding == binding
    )
    prior = accepted_review_patch_identity(terminal)
    _require(prior is not None)
    _require(prior == predecessor.patch_identity)
    _require(
        prior
        == patch_identity.compute_patch_identity(
            worktree,
            base_sha=str(prior.get("base_sha")),
            candidate_sha=str(prior.get("candidate_sha")),
        )
    )
    _require(
        _tree(worktree, str(terminal.get("snapshot_sha")))
        == terminal.get("snapshot_tree_sha")
    )
    _require(old_source.get("head") == prior.get("candidate_sha"))
    state = projection.slot_state(records, predecessor.generation_id, "delivery")
    _require(state.own_primary_consumed)
    digest = projection.canonical_record_digest(terminal)
    _require(state.primary_terminal_ref == digest)
    _require(
        any(
            reservation.reservation_id == terminal.get("review_reservation_id")
            and reservation.slot_kind == "primary"
            for reservation in state.reservations
        )
    )
    target = snapshot.patch_identity
    _require(prior["base_sha"] == target["base_sha"] and prior != target)
    delta = patch_identity.patch_delta_churn(
        worktree, prior, target, churn_ceiling=authority.DELTA_CUMULATIVE_LINE_CAP
    )
    _require(delta is not None and 0 < delta[0] <= authority.DELTA_CUMULATIVE_LINE_CAP)
    authority.bounded_delta_review_patch_bytes(
        worktree,
        delta_from=str(terminal["snapshot_sha"]),
        current_sha=snapshot.commit_sha,
    )
    link = review_state.GenerationLinkV1(
        binding, predecessor.generation_id, prior, target, "substantive"
    )
    edges = projection.generation_links(records)
    related_ids = {
        gen.generation_id
        for gen in generations.values()
        if gen.lineage_id == predecessor.lineage_id
    }
    related = [edge for edge in edges if edge.predecessor_generation_id in related_ids]
    # A durable link owns the successor even before admission is appended.
    # Exact crash retry is allowed; duplicate or different edges fail closed.
    _require(not related or (len(related) == 1 and related[0] == link))
    _require(not any(edge.to_identity == target and edge != link for edge in edges))
    for gen_id in related_ids:
        generation = generations[gen_id]
        _require(
            gen_id == predecessor.generation_id or generation.patch_identity == target
        )
        for family in ("delivery", "trust"):
            capacity = projection.slot_state(records, gen_id, family)
            _require(not capacity.delta_consumed)
            _require(not any(res.slot_kind == "delta" for res in capacity.reservations))
            _require(capacity.outstanding is None)
    return link, bool(related)


def admit_linked_delta(
    repository: Path,
    *,
    worktree: Path,
    task: Mapping[str, object],
    expected_source: Mapping[str, object],
    snapshot: ReviewSnapshot,
    load_records: Callable[[], Sequence[dict[str, object]]],
    append_records: Callable[[Sequence[Mapping[str, object]]], None],
    required_sections: tuple[str, ...],
) -> admission.Admission:
    """Derive, persist and admit under writer ownership and the authority lock.

    Adapters must durably sign/append exactly the supplied rows, preserve verified
    AuthorityRecordView on load, and propagate partial-write failures. No launch
    may occur before this function returns a new Reserved result. A persisted
    reservation whose caller was interrupted stays held for normal reconciliation.
    """
    with (
        worktree_lease.worktree_lease(worktree, boundary="review-generation-link"),
        authority_store.authority_ledger_lock(repository),
    ):
        binding = authority_store.authority_repository_binding(repository)
        records = load_records()
        try:
            link, existing = _derive(
                repository, worktree, records, task, expected_source, snapshot
            )
            _require(link.repository_binding == binding)
            predecessor = projection.generations(records)[
                link.predecessor_generation_id
            ]
            _require(set(predecessor.required_sections) <= set(required_sections))
            if not existing:
                _source(worktree, expected_source, snapshot, binding)
                append_records((link.to_dict(),))
                records = load_records()
            # Re-derive after persistence, including source and capacity. A
            # storage adapter that failed to authenticate the edge cannot launch.
            verified, persisted = _derive(
                repository, worktree, records, task, expected_source, snapshot
            )
            _require(persisted and verified == link)
            result = admission.admit_review(
                repository,
                records,
                repository_binding=authority_store.authority_repository_binding(
                    repository
                ),
                task=task,
                current_source_identity=expected_source,
                current_tree_sha=snapshot.tree_sha,
                patch_identity=snapshot.patch_identity,
                required_sections=required_sections,
                equivalence_proof=None,
                format_only_proof=None,
                requested="delta",
                changed_paths=None,
                security_trigger_paths=(),
            )
            if not isinstance(result, admission.Reserved) or result.slot.existing:
                return admission.Blocked(
                    "missing-evidence", {"cause_class": "LinkAdmissionRefused"}
                )
            _require(
                result.generation.predecessor_id == link.predecessor_generation_id
                and result.slot.slot_kind == "delta"
            )
            _source(worktree, expected_source, snapshot, binding)
            append_records(result.records_to_append)
            _source(worktree, expected_source, snapshot, binding)
            persisted_records = load_records()
            durable = {
                projection.canonical_record_digest(
                    {
                        key: value
                        for key, value in row.items()
                        if key != "terminal_authority_proof"
                    }
                )
                for row in projection.authenticated_review_state_records(
                    persisted_records
                )
            }
            _require(
                all(
                    projection.canonical_record_digest(dict(row)) in durable
                    for row in result.records_to_append
                )
            )
            generation_id = result.generation.generation_id
            _require(
                projection.generations(persisted_records).get(generation_id)
                == result.generation
            )
            coverage = projection.family_coverages(persisted_records).get(
                (generation_id, "delivery")
            )
            _require(
                coverage is not None
                and projection.canonical_record_digest(coverage.to_dict())
                == result.slot.coverage_digest
            )
            persisted_state = projection.slot_state(
                persisted_records, generation_id, "delivery"
            )
            _require(persisted_state.outstanding == result.slot)
            _require(persisted_state.settlement_for(result.slot.reservation_id) is None)
            _source(worktree, expected_source, snapshot, binding)
            # Persistence is complete; callers must not append these rows again.
            return replace(result, records_to_append=())
        except (LinkRefused, patch_identity.PatchIdentityError, DispatchError):
            return admission.Blocked("missing-evidence", {"cause_class": "LinkRefused"})
