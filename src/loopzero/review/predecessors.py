"""Finding resolution lineage; historical terminals never regain review coverage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ..kernel.authority_projection import authenticated_coordinator_record_ids
from ..kernel.run_identity import run_delivery_contract
from ..kernel.run_log import load_entries
from . import authority


def _loopzero_run(repo: Path, run_id: object) -> bool:
    if not isinstance(run_id, str) or not run_id:
        return False
    rows = load_entries(repo / ".audit" / "skill-runs")
    if not any(row.get("run_id") == run_id for row in rows):
        return False
    try:
        return run_delivery_contract(rows, run_id) == "loop-zero-v1"
    except ValueError:
        return False


def resolved_review_predecessors(
    repo: Path,
    records: Sequence[dict[str, object]],
    *,
    current_review_task_id: str | None,
) -> dict[str, dict[str, object]]:
    """Derive exact resolved producers from authority, never caller projections."""
    terminals = authority.authenticated_review_terminals(records)
    delta_ids = _delta_predecessors(
        repo,
        terminals=terminals,
        current_review_task_id=current_review_task_id,
        verdicts=authority.authenticated_verdicts(records),
    )
    resolved = {task_id: terminals[task_id] for task_id in delta_ids}
    resolved.update(
        _full_replacement_predecessors(
            repo, records, current_review_task_id=current_review_task_id
        )
    )
    return resolved


def resolved_loopzero_predecessors(
    repo: Path,
    *,
    current_review_task_id: str | None,
    records: Sequence[dict[str, object]] | None = None,
    terminals: Mapping[str, dict[str, object]] | None = None,
    verdicts: Mapping[str, dict[str, object]] | None = None,
) -> set[str]:
    """Records-based resolution, with the historical delta-only adapter retained."""
    if records is not None:
        return set(
            resolved_review_predecessors(
                repo, records, current_review_task_id=current_review_task_id
            )
        )
    return _delta_predecessors(
        repo,
        terminals=terminals or {},
        current_review_task_id=current_review_task_id,
        verdicts=verdicts or {},
    )


def _delta_predecessors(
    repo: Path,
    *,
    terminals: Mapping[str, dict[str, object]],
    current_review_task_id: str | None,
    verdicts: Mapping[str, dict[str, object]],
) -> set[str]:
    """A passing same-chain delta replaces manual closure of its predecessor."""
    resolved_predecessors: set[str] = set()
    current = terminals.get(current_review_task_id or "")
    if current is not None and isinstance(current.get("run_id"), str):
        rows = load_entries(repo / ".audit" / "skill-runs")
        run_id = str(current["run_id"])
        if any(row.get("run_id") == run_id for row in rows) and _loopzero_run(
            repo, run_id
        ):
            contract = current.get("task_contract")
            verdict = verdicts.get(current_review_task_id or "", {})
            if isinstance(contract, Mapping) and verdict.get("verdict") == "pass":
                predecessor = contract.get("delta_from_snapshot")
                # The accepted delta reviews all original findings. Its current
                # material findings block below; the old result remains raw
                # history, without a second finding-closure workflow.
                for prior_id, prior in terminals.items():
                    prior_contract = prior.get("task_contract")
                    if (
                        isinstance(predecessor, str)
                        and prior.get("snapshot_sha") == predecessor
                        and prior.get("run_id") == run_id
                        and isinstance(prior_contract, Mapping)
                        and prior_contract.get("review_chain_id")
                        == contract.get("review_chain_id")
                        and contract.get("review_chain_id")
                    ):
                        resolved_predecessors.add(prior_id)
    return resolved_predecessors


def _lineage_value(row, key):
    contract = row.get("task_contract")
    nested = contract.get(key) if isinstance(contract, Mapping) else None
    value = row.get(key, nested)
    if nested is not None and value != nested:
        return ""
    return value


def _same_lineage(prior, current):
    before, after = prior.get("task_contract"), current.get("task_contract")
    return (
        isinstance(before, Mapping)
        and isinstance(after, Mapping)
        and isinstance(before.get("review_chain_id"), str)
        and bool(before["review_chain_id"])
        and bool(before.get("required_sections"))
        and all(
            before.get(key) == after.get(key)
            for key in (
                "review_chain_id",
                "required_sections",
                "review_intent",
            )
        )
        and authority.review_gate_lens(prior) == authority.review_gate_lens(current)
        and bool(prior.get("run_id"))
        and prior.get("run_id") == current.get("run_id")
        and all(
            _lineage_value(prior, key) == _lineage_value(current, key)
            and (_lineage_value(prior, key) is None or bool(_lineage_value(prior, key)))
            for key in ("root_work_unit_id", "delivery_family_id", "slice_id")
        )
    )


def _same_review(prior, current):
    return (
        _same_lineage(prior, current)
        and isinstance(prior.get("family"), str)
        and bool(prior["family"])
        and prior.get("family") == current.get("family")
        and bool(prior.get("repository_binding"))
        and prior.get("repository_binding") == current.get("repository_binding")
    )


def _full_replacement_predecessors(repo, records, *, current_review_task_id):
    """Derive a single-use edge from signed FL-2 supersession and registration."""
    current = authority.accepted_review_terminals(records).get(current_review_task_id)
    if (
        current is None
        or authority.authenticated_verdicts(records)
        .get(current_review_task_id, {})
        .get("verdict")
        != "pass"
    ):
        return {}
    contract = current.get("task_contract")
    source = current.get("source_identity")
    patch = current.get("patch_identity")
    # Synthetic review commits bind to source by their authenticated tree.
    if (
        not isinstance(contract, Mapping)
        or "delta_from_snapshot" in contract
        or not isinstance(source, Mapping)
        or not source.get("head")
        or not source.get("ref")
        or not isinstance(patch, Mapping)
        or patch.get("candidate_sha") != source.get("head")
        or not current.get("snapshot_tree_sha")
        or patch.get("candidate_tree_sha") != current.get("snapshot_tree_sha")
        or not _loopzero_run(repo, current.get("run_id"))
    ):
        return {}
    accepted = authority.accepted_review_terminals(records)
    passing = authority.authenticated_verdicts(records)
    if (
        sum(
            _same_review(other, current)
            and other.get("source_identity") == source
            and passing.get(task_id, {}).get("verdict") == "pass"
            for task_id, other in accepted.items()
        )
        != 1
    ):
        return {}
    all_terminals = authority.authenticated_review_terminals(
        records, _superseded_task_ids=frozenset()
    )
    coordinator_ids = authenticated_coordinator_record_ids(records)
    positions = {id(row): index for index, row in enumerate(records)}
    supersessions = authority.authenticated_supersessions(records)
    resolved = {}
    for supersession in supersessions:
        task_id = supersession.get("superseded_task_id")
        prior = all_terminals.get(task_id)
        if (
            prior is None
            or prior.get("status") != "completed"
            or prior.get("advisory") is True
            or not _same_review(prior, current)
            or id(supersession) not in coordinator_ids
            or supersession.get("supersession_reason") != "stale-source"
            or supersession.get("superseding_source_identity") != source
            or not isinstance(prior.get("source_identity"), Mapping)
            or prior["source_identity"].get("ref") != source.get("ref")
            or prior["source_identity"] == source
            or any(
                supersession.get(key) != prior.get(key)
                for key in (
                    "attempt_index",
                    "snapshot_sha",
                    "snapshot_tree_sha",
                    "source_identity",
                    "task_contract_hash",
                    "result_artifact",
                    "result_sha256",
                )
            )
            or not authority._archive_validator()(
                supersession.get("uncarryable_delta_authority"),
                predecessor_task_id=task_id,
                predecessor_snapshot_sha=prior.get("snapshot_sha"),
                current_source=source,
            )
            or sum(row.get("superseded_task_id") == task_id for row in supersessions)
            != 1
        ):
            continue
        # An FL-2 edge belongs to the first matching registered full review.
        # Later tasks cannot replay it, even if the first terminal is superseded.
        starts = [
            row
            for row in records
            if row.get("type") == "attempt-start"
            and id(row) in coordinator_ids
            and _same_lineage(prior, row)
            and row.get("source_identity") == source
            and positions[id(row)] > positions[id(supersession)]
        ]
        if (
            not starts
            or starts[0].get("task_id") != current_review_task_id
            or starts[0].get("task_contract_hash") != current.get("task_contract_hash")
            or not positions[id(prior)]
            < positions[id(supersession)]
            < positions[id(starts[0])]
            < positions[id(current)]
        ):
            continue
        resolved[task_id] = prior
    return resolved if len(resolved) == 1 else {}


def publication_predecessor_terminal(
    records: Sequence[dict[str, object]],
    repo: Path | str,
    *,
    task_id: str,
    head: str | None = None,
) -> dict[str, object] | None:
    """Return only the exact resolved producer, never arbitrary historical debt."""
    matches = []
    for current_id, current in authority.accepted_review_terminals(records).items():
        if head is not None and current.get("snapshot_sha") != head:
            continue
        resolved = resolved_review_predecessors(
            Path(repo), records, current_review_task_id=current_id
        )
        if task_id in resolved:
            matches.append(resolved[task_id])
    return matches[0] if len(matches) == 1 else None
