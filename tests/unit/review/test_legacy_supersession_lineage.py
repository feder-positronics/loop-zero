"""Legacy null lineage can be validated without rewriting signed history."""

import copy

import pytest

from loopzero.kernel import gitscope
from loopzero.review import authority, provisional_findings as provisional
from loopzero.review.predecessors import resolved_review_predecessors
from tests.unit.review.test_full_replacement_lineage import (
    replacement,
    unsigned,
)
from tests.unit.review.test_ordinary_provisional_findings import (
    configured,
    finish,
    ordinary as original_ordinary,
)


@pytest.fixture
def ordinary(original_ordinary, request):
    repo, coordinator, dispatcher, records, completion = original_ordinary
    start = next(row for row in records if row["type"] == "attempt-start")
    payload = unsigned(start)
    payload["terminal_authority"] = dispatcher.registration()
    if getattr(request, "param", None):
        payload["lineage"] = request.param
    payload.update(root_work_unit_id="rc_" + "1" * 20, unit_attempt_number=1)
    payload["task_contract"].update(
        work_unit_id=payload["work_unit_id"],
        root_work_unit_id=payload["root_work_unit_id"],
    )
    payload["task_contract_hash"] = gitscope.task_contract_hash(
        payload["task_contract"]
    )
    records[records.index(start)] = coordinator.seal(
        payload, authority_kind="coordinator"
    )
    completion = dispatcher.seal(
        provisional.build_producer_completion(
            records,
            repo=repo,
            task_id=payload["task_id"],
            result_artifact=completion["result_artifact"],
            result_sha256=completion["result_sha256"],
        ),
        authority_kind="dispatcher",
    )
    return repo, coordinator, dispatcher, records, completion


def resolve(records):
    from loopzero.review.supersession import effective_supersession_lineage

    return effective_supersession_lineage(records, task_id="review-1")


def test_legacy_null_lineage_derives_without_changing_history(ordinary):
    _, _, _, records, _ = ordinary
    admission, _, terminal = finish(ordinary)
    before = copy.deepcopy(records)
    assert terminal.get("lineage") is None
    assert admission.get("lineage") is None
    assert resolve(records) == {
        key: terminal[key]
        for key in (
            "task_id",
            "work_unit_id",
            "root_work_unit_id",
            "unit_attempt_number",
            "task_contract_hash",
        )
    }
    assert records == before


@pytest.mark.parametrize(
    "field",
    [
        "task_id",
        "work_unit_id",
        "root_work_unit_id",
        "run_id",
        "unit_attempt_number",
        "attempt_index",
        "task_contract_hash",
        "source_identity",
        "snapshot_sha",
        "snapshot_tree_sha",
        "patch_identity",
        "result_artifact",
        "result_sha256",
        "task_contract",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "mismatched", "forged"])
def test_identity_drift_fails_closed(ordinary, field, mutation):
    _, _, dispatcher, records, _ = ordinary
    _, _, terminal = finish(ordinary)
    payload = unsigned(terminal)
    if mutation == "missing":
        payload.pop(field, None)
    else:
        payload[field] = "mismatch"
    records[-1] = (
        payload
        if mutation == "forged"
        else dispatcher.seal(payload, authority_kind="dispatcher")
    )
    assert resolve(records) is None


@pytest.mark.parametrize("kind", ["start", "terminal", "capture"])
def test_missing_or_ambiguous_producer_fails_closed(ordinary, kind):
    _, _, _, records, _ = ordinary
    admission, _, terminal = finish(ordinary)
    row = {
        "start": next(r for r in records if r["type"] == "attempt-start"),
        "terminal": terminal,
        "capture": admission,
    }[kind]
    # Removing a referenced capture cannot turn a failed proof into legacy success.
    assert resolve([r for r in records if r is not row]) is None
    assert resolve([*records, copy.deepcopy(row)]) is None


@pytest.mark.parametrize(
    "ordinary", [{"historical": "consumer-specific-shape"}], indirect=True
)
def test_existing_authenticated_lineage_is_returned_unchanged(ordinary):
    _, _, _, records, _ = ordinary
    _, _, terminal = finish(ordinary)
    assert resolve(records) == terminal["lineage"]
    records[-1]["lineage"]["historical"] = "forged"
    assert resolve(records) is None


def test_supersession_keeps_exact_legacy_capture_and_resolves_replacement(replacement):
    repo, _, _, records, admission, receipt, prior, current = replacement
    supersession = next(r for r in records if r["type"] == "attempt-supersession")
    history = records[: records.index(supersession)]
    before = copy.deepcopy(history)
    assert resolve(history)
    assert history == before
    assert supersession.get("lineage") is None
    assert authority.authenticated_supersessions(records) == [supersession]
    assert resolved_review_predecessors(
        repo, records, current_review_task_id=current["task_id"]
    ) == {prior["task_id"]: prior}
    assert (
        provisional.capture_provisional_findings(
            repo, authority_records=records, admission=admission
        )
        == receipt
    )


@pytest.mark.parametrize("kind", ["start", "capture"])
@pytest.mark.parametrize("mutation", ["unsigned", "wrong-key", "result-drift"])
def test_forged_registration_or_capture_is_not_authority(ordinary, kind, mutation):
    from loopzero.kernel.authority import TerminalAuthority

    _, coordinator, _, records, _ = ordinary
    admission, _, _ = finish(ordinary)
    row = (
        admission
        if kind == "capture"
        else next(r for r in records if r["type"] == "attempt-start")
    )
    payload = unsigned(row)
    if mutation == "result-drift":
        payload["source_identity"] = {"head": "0" * 40}
        changed = coordinator.seal(payload, authority_kind="coordinator")
    elif mutation == "wrong-key":
        changed = TerminalAuthority.generate().seal(
            payload, authority_kind="dispatcher"
        )
    else:
        changed = payload
    records[records.index(row)] = changed
    assert resolve(records) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "contract-hash",
        "contract-root",
        "contract-work-unit",
        "source-head",
        "patch-head",
        "patch-tree",
        "zero-attempt",
        "bool-attempt",
        "empty-root",
        "empty-lineage",
        "write-task",
        "advisory",
    ],
)
def test_even_coherently_signed_rows_require_valid_identity(ordinary, mutation):
    _, coordinator, dispatcher, records, _ = ordinary
    admission, _, terminal = finish(ordinary)
    # A historical terminal without provisional capture still has to satisfy
    # the same start/terminal consistency and structural identity checks.
    records.remove(admission)
    rows = [r for r in records if r["type"] in {"attempt-start", "attempt-terminal"}]
    for row in rows:
        payload = unsigned(row)
        for key in tuple(payload):
            if key.startswith("finding_capture") or key == "provisional_owner_id":
                payload.pop(key)
        if row["type"] == "attempt-start":
            payload["terminal_authority"] = dispatcher.registration()
        if mutation == "contract-hash":
            payload["task_contract_hash"] = "0" * 64
        elif mutation.startswith("contract-"):
            key = {
                "contract-root": "root_work_unit_id",
                "contract-work-unit": "work_unit_id",
            }[mutation]
            payload["task_contract"][key] = "other"
            payload["task_contract_hash"] = gitscope.task_contract_hash(
                payload["task_contract"]
            )
        elif mutation == "source-head":
            payload["source_identity"]["head"] = "0" * 40
        elif mutation.startswith("patch-"):
            key = "candidate_sha" if mutation == "patch-head" else "candidate_tree_sha"
            payload["patch_identity"][key] = "0" * 40
        elif mutation in {"zero-attempt", "bool-attempt"}:
            payload["unit_attempt_number"] = 0 if mutation == "zero-attempt" else True
        elif mutation == "write-task":
            payload["work_kind"] = "implementation"
            payload["read_only"] = False
        elif mutation == "advisory":
            payload["advisory"] = True
        elif mutation == "empty-root":
            payload["root_work_unit_id"] = ""
        else:
            payload["lineage"] = {}
        signer = coordinator if row["type"] == "attempt-start" else dispatcher
        kind = "coordinator" if row["type"] == "attempt-start" else "dispatcher"
        records[records.index(row)] = signer.seal(payload, authority_kind=kind)
    assert resolve(records) is None
