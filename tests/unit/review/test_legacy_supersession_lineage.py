"""Legacy null lineage can be validated without rewriting signed history."""

import copy
import subprocess

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


@pytest.fixture
def synthetic_snapshot(ordinary):
    repo, coordinator, dispatcher, records, completion = ordinary
    start = next(row for row in records if row["type"] == "attempt-start")
    source = start["source_identity"]["head"]
    tree = start["snapshot_tree_sha"]
    snapshot = (
        subprocess.check_output(
            ["git", "commit-tree", tree, "-p", source],
            input=b"synthetic review snapshot\n",
            cwd=repo,
        )
        .decode()
        .strip()
    )
    assert snapshot != source
    assert (
        subprocess.check_output(
            ["git", "rev-parse", f"{snapshot}^{{tree}}"],
            cwd=repo,
        )
        .decode()
        .strip()
        == tree
    )
    payload = unsigned(start)
    payload.update(snapshot_sha=snapshot, terminal_authority=dispatcher.registration())
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


def test_synthetic_commit_with_same_authenticated_tree_derives_lineage(
    synthetic_snapshot,
):
    repo, _, _, records, _ = synthetic_snapshot
    admission, receipt, terminal = finish(synthetic_snapshot)
    assert authority.authenticated_review_terminals(records)["review-1"] is terminal
    assert (
        provisional.authenticated_capture_admissions(records)["review-1"] is admission
    )
    assert terminal["snapshot_sha"] != terminal["source_identity"]["head"]
    assert (
        terminal["patch_identity"]["candidate_sha"]
        == terminal["source_identity"]["head"]
    )
    assert (
        terminal["patch_identity"]["candidate_tree_sha"]
        == terminal["snapshot_tree_sha"]
    )
    before = copy.deepcopy(records)
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
    assert (
        provisional.capture_provisional_findings(
            repo,
            authority_records=records,
            admission=admission,
        )
        == receipt
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "different-tree",
        "candidate-sha",
        "candidate-tree",
        "missing-patch",
        "forged-patch",
        "missing-head",
        "invalid-head",
        "empty-head",
        "ambiguous",
    ],
)
def test_synthetic_snapshot_binding_fails_closed(synthetic_snapshot, mutation):
    repo, coordinator, dispatcher, records, _ = synthetic_snapshot
    admission, _, terminal = finish(synthetic_snapshot)
    records.remove(admission)
    # Exercise the helper's own guards with an authenticated uncaptured legacy
    # terminal, so upstream capture checks cannot mask a broken source binding.
    original_source = terminal["source_identity"]["head"]
    other_tree = None
    other_snapshot = None
    if mutation == "different-tree":
        other_tree = (
            subprocess.check_output(
                ["git", "mktree"],
                input=b"",
                cwd=repo,
            )
            .decode()
            .strip()
        )
        assert other_tree != terminal["snapshot_tree_sha"]
        other_snapshot = (
            subprocess.check_output(
                ["git", "commit-tree", other_tree, "-p", original_source],
                input=b"different snapshot tree\n",
                cwd=repo,
            )
            .decode()
            .strip()
        )
    for row in [
        r for r in records if r["type"] in {"attempt-start", "attempt-terminal"}
    ]:
        payload = unsigned(row)
        for key in tuple(payload):
            if key.startswith("finding_capture") or key == "provisional_owner_id":
                payload.pop(key)
        if row["type"] == "attempt-start":
            payload["terminal_authority"] = dispatcher.registration()
        if mutation == "different-tree":
            payload["snapshot_tree_sha"] = other_tree
            payload["snapshot_sha"] = other_snapshot
        elif mutation == "candidate-sha":
            payload["patch_identity"]["candidate_sha"] = payload["snapshot_sha"]
        elif mutation == "candidate-tree":
            payload["patch_identity"]["candidate_tree_sha"] = "0" * 40
        elif mutation == "missing-patch":
            payload.pop("patch_identity")
        elif mutation in {"missing-head", "invalid-head", "empty-head"}:
            head = {"missing-head": None, "invalid-head": "g" * 40, "empty-head": ""}[
                mutation
            ]
            payload["source_identity"]["head"] = head
            payload["patch_identity"]["candidate_sha"] = head
        signer = coordinator if row["type"] == "attempt-start" else dispatcher
        kind = "coordinator" if row["type"] == "attempt-start" else "dispatcher"
        records[records.index(row)] = signer.seal(payload, authority_kind=kind)
    if mutation == "forged-patch":
        records[-1]["patch_identity"]["candidate_sha"] = "0" * 40
    elif mutation == "ambiguous":
        records.append(copy.deepcopy(records[-1]))
    else:
        # Every signed malformed-identity case remains authenticated; only the
        # helper's explicit binding checks reject it. The control is non-vacuous.
        assert (
            authority.authenticated_review_terminals(records)["review-1"] is records[-1]
        )
    assert bool(resolve(records)) is (mutation == "none")
