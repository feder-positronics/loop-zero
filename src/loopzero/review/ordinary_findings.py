"""Registered dispatcher completion evidence for ordinary pre-PR capture.

A completion is signed by the existing attempt key and embedded in one host
admission. Neither record is an attempt terminal, verdict, or capacity event.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from ..kernel import policy
from ..kernel.authority import TerminalAuthorityError, verify_terminal_authority
from ..kernel.authority_projection import (
    AuthorityRecordView,
    _authenticated_open_dispatch_attempts,
    _authenticated_registration_start_ids,
    authenticated_coordinator_record_ids,
)
from ..kernel.gitscope import task_contract_hash
from . import provisional_findings as pf
from .chain import (
    ReviewChainError,
    build_review_chain_receipt,
    validate_review_chain_result,
)

COMPLETION_TYPE = "finding-producer-completion-v1"
ADMISSION_TYPE = "finding-capture-admission-v1"


def _view(records, selected):
    return (
        records.filtered(selected)
        if isinstance(records, AuthorityRecordView)
        else list(selected)
    )


def _producer(records, completion):
    """Verify registration and exact signed bytes before considering a digest."""
    expected_keys = set(pf._IDENTITY_FIELDS) | {
        "type",
        "status",
        "ts",
        "schema_version",
        "policy_version",
        "terminal_authority_proof",
    }
    if (
        set(completion) != expected_keys
        or completion.get("type") != COMPLETION_TYPE
        or completion.get("status") != "completed"
    ):
        raise pf.ProvisionalFindingError("ordinary completion shape is invalid")
    if (
        completion.get("schema_version") != policy.TELEMETRY_SCHEMA_VERSION
        or completion.get("policy_version") != policy.DISPATCH_POLICY_VERSION
    ):
        raise pf.ProvisionalFindingError("ordinary completion version is invalid")
    if not isinstance(completion.get("ts"), str) or not completion["ts"]:
        raise pf.ProvisionalFindingError("ordinary completion envelope is invalid")
    start = _registered_producer(records, completion.get("task_id"))
    for field in pf._IDENTITY_FIELDS:
        if field not in {"result_artifact", "result_sha256"} and completion.get(
            field
        ) != start.get(field):
            raise pf.ProvisionalFindingError(
                "ordinary completion changed registered identity"
            )
    try:
        verify_terminal_authority(
            completion,
            registration=start.get("terminal_authority"),
            expected_kind="dispatcher",
        )
    except TerminalAuthorityError as exc:
        raise pf.ProvisionalFindingError(
            "ordinary completion dispatcher proof is invalid"
        ) from exc
    return start


def _registered_producer(records, task_id):
    authenticated = _authenticated_registration_start_ids(records)
    starts = [
        row
        for row in records
        if id(row) in authenticated and row.get("task_id") == task_id
    ]
    if len(starts) != 1:
        raise pf.ProvisionalFindingError(
            "ordinary completion has no unique registration"
        )
    raw_start = starts[0]
    start = dict(raw_start)
    from ..kernel.authority_projection import (
        authenticated_review_state_records,
        slot_state,
    )
    from ..kernel.review_state import ReviewGenerationV1
    from .authority import _terminal_verdict_family

    if _terminal_verdict_family(start, require_intent=True) != (True, "delivery"):
        raise pf.ProvisionalFindingError("ordinary producer review family is invalid")
    generation_id = start.get("review_generation_id") or start.get("generation_id")
    reservation_id = start.get("review_reservation_id") or start.get("reservation_id")
    if not isinstance(generation_id, str) or not isinstance(reservation_id, str):
        raise pf.ProvisionalFindingError(
            "ordinary producer review obligation is missing"
        )
    state = slot_state(records, generation_id, "delivery")
    reservations = [
        row
        for row in state.reservations
        if row.reservation_id == reservation_id and row.task_id == start.get("task_id")
    ]
    generations = [
        row
        for row in authenticated_review_state_records(records)
        if row.get("type") == "review-generation-v1"
        and row.get("generation_id") == generation_id
    ]
    if (
        len(reservations) != 1
        or len(generations) != 1
        or state.settlement_for(reservation_id) is not None
    ):
        raise pf.ProvisionalFindingError(
            "ordinary producer has no live registered review reservation"
        )
    generation = ReviewGenerationV1.from_dict(generations[0])
    if generation.tree != start.get("snapshot_tree_sha") or dict(
        generation.patch_identity
    ) != start.get("patch_identity"):
        raise pf.ProvisionalFindingError(
            "ordinary producer source differs from its generation"
        )
    derived = {
        "reservation_id": reservation_id,
        "generation_id": generation_id,
        "family": "delivery",
        "repository_binding": generation.repository_binding,
    }
    for name, value in derived.items():
        if start.get(name) is not None and start[name] != value:
            raise pf.ProvisionalFindingError(
                "ordinary registration contradicts review authority"
            )
        start[name] = value
    if (
        start.get("delivery_contract", "loop-zero-v1") != "loop-zero-v1"
        or start.get("read_only") is not True
        or start.get("advisory") is True
        or start.get("work_kind") != "review"
        or start.get("review_intent") != "delivery-code-review"
        or type(start.get("attempt_index")) is not int
        or start["attempt_index"] < 0
        or any(
            not isinstance(start.get(key), str) or not start[key]
            for key in (
                "task_id",
                "run_id",
                "worktree",
                "repository_binding",
                "reservation_id",
                "generation_id",
                "family",
            )
        )
    ):
        raise pf.ProvisionalFindingError(
            "ordinary producer is not an eligible native review"
        )
    contract = start.get("task_contract")
    if (
        not isinstance(contract, dict)
        or task_contract_hash(contract) != start.get("task_contract_hash")
        or contract.get("review_intent") != "delivery-code-review"
    ):
        raise pf.ProvisionalFindingError("ordinary producer task contract is invalid")
    key = (start["task_id"], start["attempt_index"])
    if (
        _authenticated_open_dispatch_attempts(
            records, worktree=Path(start["worktree"])
        ).get(key)
        != raw_start
    ):
        raise pf.ProvisionalFindingError(
            "ordinary completion attempt is already closed"
        )
    # Retain conservative rejection for a signed retirement even if it cannot
    # be projected as a normal review terminal; no new owner after retirement.
    coordinator_ids = authenticated_coordinator_record_ids(records)
    if any(
        id(row) in coordinator_ids
        and row.get("type") == "attempt-supersession"
        and (
            row.get("task_id") == start["task_id"]
            or row.get("superseded_task_id") == start["task_id"]
        )
        for row in records
    ):
        raise pf.ProvisionalFindingError("ordinary completion producer is superseded")
    return start


def build_producer_completion(
    records: Sequence[dict[str, object]],
    *,
    repo: Path | str,
    task_id: str,
    result_artifact: str,
    result_sha256: str,
) -> dict[str, object]:
    """Build exact bytes for the registered dispatcher to sign after execution.

    This unsigned payload is not admission or execution evidence. Only its
    registered dispatcher signature can support the coordinator transition.
    """
    from datetime import UTC, datetime

    start = _registered_producer(records, task_id)
    identity = {field: start.get(field) for field in pf._IDENTITY_FIELDS}
    identity.update(result_artifact=result_artifact, result_sha256=result_sha256)
    _validated_result(repo, {**identity, "task_contract": start["task_contract"]})
    return {
        **identity,
        "type": COMPLETION_TYPE,
        "status": "completed",
        "ts": datetime.now(UTC).isoformat(),
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }


def _payload(start, completion):
    identity = {field: completion.get(field) for field in pf._IDENTITY_FIELDS}
    return {
        "type": ADMISSION_TYPE,
        "status": "admitted",
        "delivery_contract": "loop-zero-v1",
        "provisional_owner_id": "pfo_"
        + pf._digest({"domain": ADMISSION_TYPE, **identity}),
        **identity,
        "task_contract": start["task_contract"],
        "producer_completion": dict(completion),
    }


def authenticated_capture_admissions(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    coordinator_ids = authenticated_coordinator_record_ids(records)
    result = {}
    for index, row in enumerate(records):
        if row.get("type") != ADMISSION_TYPE or id(row) not in coordinator_ids:
            continue
        completion = row.get("producer_completion")
        if not isinstance(completion, Mapping):
            continue
        try:
            start = _producer(_view(records, records[:index]), completion)
            expected = _payload(start, completion)
        except (pf.ProvisionalFindingError, TypeError, ValueError):
            continue
        if pf._matches_enveloped_payload(row, expected):
            task = str(row["task_id"])
            # First authenticated owner wins; no later row can replace it.
            result.setdefault(task, row)
    return result


def authenticated_provisional_admissions(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    result = dict(pf.authenticated_recovery_admissions(records))
    for task, row in authenticated_capture_admissions(records).items():
        if task in result:
            # Different provenance cannot silently move a task's owner.
            result.pop(task)
        else:
            result[task] = row
    return result


def _validated_result(repo, admission):
    from ..kernel.run_identity import run_delivery_contract
    from ..kernel.run_log import load_entries

    root = pf._root(repo)
    try:
        contract = run_delivery_contract(
            load_entries(root / pf._PROVISIONAL_DIR.parent / "skill-runs"),
            str(admission.get("run_id")),
        )
    except (OSError, ValueError) as exc:
        raise pf.ProvisionalFindingError(
            "ordinary producer native run is unavailable"
        ) from exc
    if contract != "loop-zero-v1":
        raise pf.ProvisionalFindingError("ordinary producer run is not native")
    result = pf.load_recovery_result(repo, admission)
    if result.get("status") != "completed" or result.get("task_id") != admission.get(
        "task_id"
    ):
        raise pf.ProvisionalFindingError(
            "ordinary result completion identity is invalid"
        )
    try:
        validate_review_chain_result(result, task=admission["task_contract"])
    except (ReviewChainError, KeyError) as exc:
        raise pf.ProvisionalFindingError(
            "ordinary result sections are invalid"
        ) from exc
    pf._authenticated_snapshot(pf._root(repo), admission)
    return result


def build_capture_admission(
    records: Sequence[dict[str, object]],
    *,
    repo: Path | str,
    completion: Mapping[str, object],
) -> dict[str, object]:
    existing = authenticated_capture_admissions(records).get(
        str(completion.get("task_id"))
    )
    if existing is not None:
        if existing.get("producer_completion") != completion:
            raise pf.ProvisionalFindingError(
                "ordinary capture replay changed completion"
            )
        _validated_result(repo, existing)
        return pf._semantic_authority_payload(existing)
    if any(
        row.get("type") in {ADMISSION_TYPE, pf.ADMISSION_TYPE}
        and row.get("task_id") == completion.get("task_id")
        for row in records
    ):
        raise pf.ProvisionalFindingError("ordinary capture has conflicting admission")
    start = _producer(records, completion)
    admission = _payload(start, completion)
    _validated_result(repo, admission)
    return admission


def authorize_capture_admission_append(
    repo: Path | str,
    records: Sequence[dict[str, object]],
    prospective: Mapping[str, object],
) -> bool:
    """Validate the sealed admission while the caller holds both writer locks.

    Keep the authority-ledger and original task lifecycle locks held through
    the consumer's durable append. False means exact replay: append nothing.
    """
    from ..kernel.authority_store import assert_attempt_lifecycle_lock_held
    from ..kernel.review_state import assert_authority_ledger_lock_held

    assert_authority_ledger_lock_held(pf._root(repo))
    assert_attempt_lifecycle_lock_held(prospective.get("task_id"))
    completion = prospective.get("producer_completion")
    if not isinstance(completion, Mapping):
        raise pf.ProvisionalFindingError("ordinary capture completion is missing")
    expected = build_capture_admission(records, repo=repo, completion=completion)
    combined = _view(records, [*records, dict(prospective)])
    candidate = combined[-1]
    if id(candidate) not in authenticated_coordinator_record_ids(
        combined
    ) or not pf._matches_enveloped_payload(candidate, expected):
        raise pf.ProvisionalFindingError("ordinary capture admission proof is invalid")
    existing = authenticated_capture_admissions(records).get(
        str(prospective.get("task_id"))
    )
    return existing is None


def build_capture_terminal_evidence(
    records: Sequence[dict[str, object]],
    *,
    repo: Path | str,
    admission: Mapping[str, object],
    capture_receipt: Mapping[str, object],
) -> dict[str, object]:
    if (
        authenticated_capture_admissions(records).get(str(admission.get("task_id")))
        != admission
    ):
        raise pf.ProvisionalFindingError("ordinary capture owner is not authenticated")
    result = _validated_result(repo, admission)
    receipt = pf.capture_provisional_findings(
        repo, authority_records=records, admission=admission
    )
    if dict(capture_receipt) != receipt:
        raise pf.ProvisionalFindingError("ordinary capture receipt changed")
    try:
        chain = build_review_chain_receipt(
            task={**admission["task_contract"], "task_id": admission["task_id"]},
            snapshot_sha=admission["snapshot_sha"],
            snapshot_tree_sha=admission["snapshot_tree_sha"],
            patch_identity=admission.get("patch_identity"),
            result=result,
            finding_ids=receipt["finding_ids"],
        )
    except ReviewChainError as exc:
        raise pf.ProvisionalFindingError("ordinary capture chain is invalid") from exc
    return {
        **{field: admission.get(field) for field in pf._IDENTITY_FIELDS},
        "provisional_owner_id": admission["provisional_owner_id"],
        "finding_capture_receipt": receipt,
        "finding_capture_receipt_sha256": pf._digest(receipt),
        "review_chain_receipt": chain,
    }


def ordinary_terminal_matches(
    records: Sequence[dict[str, object]], terminal: Mapping[str, object]
) -> bool:
    """Validate the signed terminal's capture join without changing settlement."""
    admissions = authenticated_capture_admissions(records)
    admission = admissions.get(str(terminal.get("task_id")))
    if admission is None:
        return terminal.get("provisional_owner_id") is None
    if (
        terminal.get("type") != "attempt-terminal"
        or terminal.get("status") != "completed"
    ):
        return True  # Failed terminals remain failures; they confer no verdict.
    if any(
        terminal.get(field) != admission.get(field) for field in pf._IDENTITY_FIELDS
    ):
        return False
    receipt = terminal.get("finding_capture_receipt")
    if not isinstance(receipt, Mapping):
        return False
    try:
        result = _validated_result(Path(str(admission["worktree"])), admission)
        persisted = pf._capture_receipts(
            pf._stream_records(Path(str(admission["worktree"])))
        )
        if persisted.get(admission["provisional_owner_id"]) != receipt:
            return False
        expected_chain = build_review_chain_receipt(
            task={**admission["task_contract"], "task_id": admission["task_id"]},
            snapshot_sha=admission["snapshot_sha"],
            snapshot_tree_sha=admission["snapshot_tree_sha"],
            patch_identity=admission.get("patch_identity"),
            result=result,
            finding_ids=receipt["finding_ids"],
        )
        return (
            terminal.get("provisional_owner_id") == admission["provisional_owner_id"]
            and terminal.get("finding_capture_receipt_sha256") == pf._digest(receipt)
            and terminal.get("review_chain_receipt") == expected_chain
        )
    except (
        pf.ProvisionalFindingError,
        ReviewChainError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False
