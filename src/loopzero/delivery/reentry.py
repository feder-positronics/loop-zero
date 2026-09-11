"""One append-only review reentry and its publication generation."""

from collections.abc import Mapping, Sequence
from pathlib import Path


class StaleReentryProjection(RuntimeError):
    """A durable reentry decision whose run already left ci-wait."""


def reentry_decisions(
    records: Sequence[dict[str, object]], run_id: object
) -> list[dict[str, object]]:
    """Return the authenticated review-reentry decisions of one run."""
    from ..review.authority import delivery_controller_records

    return [
        row
        for row in delivery_controller_records(records)
        if row.get("type") == "delivery-control"
        and row.get("action") == "review-reentry"
        and row.get("run_id") == run_id
    ]


def publication_generation(records: Sequence[dict[str, object]], run_id: object) -> int:
    """Bind publication to the authenticated reentry history, even at the same head.

    Callers pass the authority records they already froze; the trust root is
    never derived from an evidence directory path.
    """
    return len(reentry_decisions(records, run_id))


def require_review_after_reentry(
    records: Sequence[dict[str, object]],
    *,
    run_id: str,
    review_task_ids: Sequence[str],
) -> None:
    """Refuse a reminted publication that cites review evidence from before reentry."""
    from ..review.authority import accepted_review_producers

    decisions = reentry_decisions(records, run_id)
    if not decisions:
        return
    # Authority projections preserve the original record objects and ledger order.
    # Wall-clock timestamps cannot establish order across a clock adjustment.
    positions = {id(record): index for index, record in enumerate(records)}
    decided_positions = [positions.get(id(row)) for row in decisions]
    if any(position is None for position in decided_positions):
        raise ValueError("review reentry decision is absent from the frozen ledger")
    decided_at = max(position for position in decided_positions if position is not None)
    terminals = accepted_review_producers(records)
    for task_id in dict.fromkeys(review_task_ids):
        accepted_at = positions.get(id(terminals.get(task_id)))
        if accepted_at is None or accepted_at <= decided_at:
            raise ValueError(
                f"review reentry invalidated review task {task_id!r}; publication "
                "requires review evidence accepted after the reentry decision from a post-reentry producer"
            )


def append_reentry_phase(
    *, root: Path, skill: str, run_id: str, record: Mapping[str, object]
) -> None:
    """Project a durable decision once; a retry repairs an interrupted projection.

    The projection revalidates the run's current phase so a late repair can
    never append an illegal event to the append-only phase history.
    """
    from ..kernel.events import common_fields, write_unique_event
    from ..kernel.run_log import load_phase_events, phase_state

    events = load_phase_events(root)
    if any(
        row.get("run_id") == run_id and row.get("status") == "review-reentry"
        for row in events
    ):
        return
    if phase_state(events, skill=skill, run_id=run_id)[0] != "ci-wait":
        raise StaleReentryProjection(
            "review reentry decision is durable but the run left ci-wait before "
            "its projection; recover the run explicitly instead of retrying"
        )
    event = {
        **common_fields(),
        "kind": "phase",
        "skill": skill,
        "run_id": run_id,
        "phase": "3",
        "name": "review",
        "status": "review-reentry",
        "reason": record["reason"],
    }
    if not write_unique_event(event, key_fields=("kind", "run_id", "status")):
        raise ValueError("review reentry phase could not be written durably")


def pending_reentry(
    records: Sequence[dict[str, object]],
    events: Sequence[dict[str, object]],
    run_id: str,
) -> dict[str, object] | None:
    """Return only a durable decision whose phase projection was interrupted."""
    if any(
        row.get("run_id") == run_id and row.get("status") == "review-reentry"
        for row in events
    ):
        return None
    return next(
        (
            row
            for row in records
            if row.get("type") == "delivery-control"
            and row.get("action") == "review-reentry"
            and row.get("run_id") == run_id
        ),
        None,
    )
