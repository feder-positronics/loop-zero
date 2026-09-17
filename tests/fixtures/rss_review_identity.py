# Read-only extraction of IntelFlo's derived full-review identity path.
from __future__ import annotations
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import cast

CONTROL_TYPE = "delivery-control"
MAX_SCOPE_TRANSITIONS = 2
RUN_ID_RE = re.compile(r"sr_[0-9a-f]{32}")

class DeliveryControlError(ValueError):
    pass

def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"

def _validate_run_id(run_id: str) -> None:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise DeliveryControlError("run_id must match sr_<32 lowercase hex>")

def review_identity(run_id: str, *, transition_ordinal: int) -> dict[str, str]:
    """Return one authority-derived identity for an outer delivery run."""
    _validate_run_id(run_id)
    if transition_ordinal < 0 or transition_ordinal > MAX_SCOPE_TRANSITIONS:
        raise DeliveryControlError("review family permits at most two transitions")
    family = _stable_id("rf", "delivery-family-v1", run_id)
    slice_id = _stable_id("rs", "delivery-slice-v1", family, 0)
    chain = _stable_id("rc", "review-chain-v1", slice_id, transition_ordinal)
    return {
        "delivery_family_id": family,
        "slice_id": slice_id,
        "review_chain_id": chain,
        "root_work_unit_id": chain,
    }

def _control_records(
    records: Sequence[dict[str, object]], *, run_id: str, action: str | None = None
) -> list[dict[str, object]]:
    return [
        record
        for record in records
        if record.get("type") == CONTROL_TYPE
        and record.get("run_id") == run_id
        and (action is None or record.get("action") == action)
    ]

def transition_ordinal(records: Sequence[dict[str, object]], *, run_id: str) -> int:
    """Return the last contiguous dispatcher-minted scope transition."""
    _validate_run_id(run_id)
    transitions = _control_records(records, run_id=run_id, action="scope-transition")
    ordinals = [record.get("transition_ordinal") for record in transitions]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in ordinals):
        raise DeliveryControlError("scope-transition record has an invalid ordinal")
    normalized = cast(list[int], ordinals)
    expected = list(range(1, len(normalized) + 1))
    if normalized != expected or len(normalized) > MAX_SCOPE_TRANSITIONS:
        raise DeliveryControlError("scope-transition history is non-contiguous")
    return len(normalized)

def bind_review_identity(
    task: dict[str, object],
    *,
    records: Sequence[dict[str, object]],
    run_id: str,
) -> dict[str, str]:
    """Bind a delivery review task to IDs the dispatcher derives itself."""
    if task.get("review_intent") != "delivery-code-review":
        return {}
    delta_from = task.get("delta_from_snapshot")
    identity = (
        _delta_predecessor_identity(records, delta_from, run_id=run_id)
        if isinstance(delta_from, str)
        else None
    )
    if identity is None:
        identity = review_identity(
            run_id,
            transition_ordinal=transition_ordinal(records, run_id=run_id),
        )
    for field, expected in identity.items():
        supplied = task.get(field)
        if supplied is not None and supplied != expected:
            raise DeliveryControlError(
                f"caller-supplied {field} conflicts with dispatcher-derived identity"
            )
        task[field] = expected
    return identity
