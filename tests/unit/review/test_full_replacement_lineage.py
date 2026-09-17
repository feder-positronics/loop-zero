"""Signed FL-2 replacement lineage and publication evidence regression probes."""

import copy
import hashlib
import json
import subprocess

import pytest
from loopzero.delivery._publish_findings import resolved_loopzero_predecessors
from loopzero.delivery.publish import open_important_finding_ids
from loopzero.kernel import authority_store, gitscope, patch_identity
from loopzero.kernel.worktree_lease import source_identity
from loopzero.review import (
    admission as review_admission,
)
from loopzero.review import (
    authority,
    chain,
)
from loopzero.review import (
    provisional_findings as provisional,
)
from tests.unit.review.test_ordinary_provisional_findings import (
    configured as configured_fixture,
)
from tests.unit.review.test_ordinary_provisional_findings import (
    finish,
)
from tests.unit.review.test_ordinary_provisional_findings import (
    ordinary as ordinary_fixture,
)

configured = configured_fixture
ordinary = ordinary_fixture


def unsigned(row):
    return {
        k: copy.deepcopy(v)
        for k, v in row.items()
        if k not in {"terminal_authority_proof", "terminal_authority"}
    }


@pytest.fixture
def replacement(ordinary, request):
    repo, coordinator, dispatcher, records, completion = ordinary
    start = next(row for row in records if row["type"] == "attempt-start")
    payload = unsigned(start)
    payload["terminal_authority"] = dispatcher.registration()
    payload["task_contract"].update(
        review_chain_id="rc_" + "1" * 20,
        root_work_unit_id="rc_" + "1" * 20,
        delivery_family_id="rf_" + "1" * 20,
        slice_id="rs_" + "1" * 20,
    )
    payload["root_work_unit_id"] = payload["task_contract"]["root_work_unit_id"]
    subprocess.run(["git", "checkout", "-qb", "fix/18"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD^"], cwd=repo, check=True
    )
    payload["source_identity"] = source_identity(repo)
    payload["task_contract_hash"] = gitscope.task_contract_hash(
        payload["task_contract"]
    )
    records[records.index(start)] = coordinator.seal(
        payload, authority_kind="coordinator"
    )
    completion_payload = unsigned(completion)
    completion_payload["task_contract_hash"] = payload["task_contract_hash"]
    completion_payload["root_work_unit_id"] = payload["root_work_unit_id"]
    completion_payload["source_identity"] = payload["source_identity"]
    completion = dispatcher.seal(completion_payload, authority_kind="dispatcher")
    admission, receipt, prior = finish(
        (repo, coordinator, dispatcher, records, completion)
    )
    (repo / "owned.py").write_text("owned = True\n" + "# changed\n" * 691)
    subprocess.run(["git", "add", "owned.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "candidate"],
        cwd=repo,
        check=True,
    )
    source = source_identity(repo)
    proof = authority.uncarryable_delta_authority(
        prior, worktree=repo, current_source=source, history=records
    )
    assert proof["reason"] == "delta-churn-exceeded"
    assert proof["cumulative_churn"] == 691
    supersession = coordinator.seal(
        {
            **unsigned(prior),
            "type": "attempt-supersession",
            "status": "superseded",
            "superseded_task_id": prior["task_id"],
            "supersession_reason": "stale-source",
            "superseding_source_identity": source,
            "uncarryable_delta_authority": proof,
        },
        authority_kind="coordinator",
    )
    records.append(supersession)
    current = unsigned(prior)
    for key in tuple(current):
        if key.startswith("finding_capture") or key == "provisional_owner_id":
            current.pop(key)
    identity = patch_identity.capture_patch_identity(repo, candidate_sha=source["head"])
    with authority_store.authority_ledger_lock(repo):
        admitted = review_admission.admit_review(
            repo,
            records,
            repository_binding=prior["repository_binding"],
            task={
                **prior["task_contract"],
                "task_id": "review-2",
                "idempotency_key": "review-2",
                "run_id": prior["run_id"],
            },
            current_source_identity=source,
            current_tree_sha=identity["candidate_tree_sha"],
            patch_identity=identity,
            required_sections=("code", "security"),
            equivalence_proof=None,
            format_only_proof=None,
            requested="review",
            changed_paths=None,
            security_trigger_paths=("owned.py",),
        )
    assert isinstance(admitted, review_admission.Reserved), admitted
    reservation = admitted.slot
    records.extend(
        coordinator.seal(row, authority_kind="coordinator")
        for row in admitted.records_to_append
    )
    current.update(
        task_id="review-2",
        work_unit_id="review-2",
        source_identity=source,
        snapshot_sha=source["head"],
        snapshot_tree_sha=identity["candidate_tree_sha"],
        patch_identity=identity,
        generation_id=reservation.generation_id,
        review_generation_id=reservation.generation_id,
        reservation_id=reservation.reservation_id,
        review_reservation_id=reservation.reservation_id,
    )
    if getattr(request, "param", None) == "synthetic":
        # Sanitized RSS shape: the source/patch commit and review snapshot
        # differ, but both Git objects contain the same authenticated tree.
        snapshot = subprocess.check_output(
            [
                "git",
                "commit-tree",
                current["snapshot_tree_sha"],
                "-p",
                source["head"],
                "-m",
                "RSS synthetic review snapshot",
            ],
            cwd=repo,
            text=True,
        ).strip()
        assert snapshot != source["head"]
        assert (
            subprocess.check_output(
                ["git", "rev-parse", snapshot + "^{tree}"], cwd=repo, text=True
            ).strip()
            == current["snapshot_tree_sha"]
        )
        current["snapshot_sha"] = snapshot
    result = {
        "review_sections": {
            "code": {"completion": "completed", "verdict": "clean", "findings": []},
            "security": {
                "completion": "completed",
                "verdict": "clean",
                "findings": [],
                "threat_model_summary": "No changed trust boundary.",
            },
        },
        "findings": [],
    }
    current["review_chain_receipt"] = chain.build_review_chain_receipt(
        task={**current["task_contract"], "task_id": current["task_id"]},
        snapshot_sha=current["snapshot_sha"],
        snapshot_tree_sha=current["snapshot_tree_sha"],
        patch_identity=current["patch_identity"],
        result=result,
        finding_ids=[],
    )
    start = {
        **current,
        "type": "attempt-start",
        "terminal_authority": dispatcher.registration(),
    }
    for key in ("family", "repository_binding", "reservation_id", "generation_id"):
        start.pop(key)
    records.append(coordinator.seal(start, authority_kind="coordinator"))
    result = {
        **json.loads((repo / prior["result_artifact"]).read_text()),
        **result,
        "task_id": "review-2",
    }
    if getattr(request, "param", False) is True:
        finding = {
            "severity": "important",
            "claim": "replacement still has a defect",
            "path": "owned.py",
            "line_start": 1,
            "line_end": 1,
        }
        result["findings"] = [finding]
        result["review_sections"]["code"].update(verdict="findings", findings=[finding])
    artifact = repo / ".audit/dispatch/results/review-2.json"
    artifact.write_text(json.dumps(result))
    completion = dispatcher.seal(
        provisional.build_producer_completion(
            records,
            repo=repo,
            task_id="review-2",
            result_artifact=str(artifact.relative_to(repo)),
            result_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ),
        authority_kind="dispatcher",
    )
    current_admission = coordinator.seal(
        {
            **provisional.build_capture_admission(
                records, repo=repo, completion=completion
            ),
            "schema_version": prior["schema_version"],
            "policy_version": prior["policy_version"],
            "ts": "2026-09-17T00:00:00+00:00",
        },
        authority_kind="coordinator",
    )
    records.append(current_admission)
    current_receipt = provisional.capture_provisional_findings(
        repo, authority_records=records, admission=current_admission
    )
    evidence = provisional.build_capture_terminal_evidence(
        records, repo=repo, admission=current_admission, capture_receipt=current_receipt
    )
    terminal = dispatcher.seal(
        {
            **current,
            **evidence,
            "result_artifact": completion["result_artifact"],
            "result_sha256": completion["result_sha256"],
        },
        authority_kind="dispatcher",
    )
    records.append(terminal)
    records.append(
        coordinator.seal(
            {
                "type": "verdict",
                "task_id": "review-2",
                "run_id": current["run_id"],
                "schema_version": current["schema_version"],
                "policy_version": current["policy_version"],
                "verdict": "pass",
                "target_worker_identity": current.get("worker_identity"),
                "verifier_identity": "human:independent",
                "verifier_alias": "human",
            },
            authority_kind="coordinator",
        )
    )
    assert authority.authenticated_supersessions(records) == [supersession]
    assert authority.authenticated_verdicts(records)["review-2"]["verdict"] == "pass"
    assert "review-1" not in authority.authenticated_review_terminals(records)
    return repo, coordinator, dispatcher, records, admission, receipt, prior, terminal


@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
def test_full_replacement_resolves_and_materializes_exact_predecessor(replacement):
    repo, coordinator, _, records, admission, receipt, prior, current = replacement
    assert resolved_loopzero_predecessors(
        repo,
        terminals=authority.authenticated_review_terminals(records),
        records=records,
        current_review_task_id="review-2",
        verdicts=authority.authenticated_verdicts(records),
    ) == {"review-1"}
    assert (
        open_important_finding_ids(
            repo,
            prior["source_identity"]["ref"],
            dispatch_records=records,
            finding_records=[],
            current_review_task_id="review-2",
        )
        == ()
    )
    binding = provisional.build_publication_binding(
        records,
        repo=repo,
        admission=admission,
        capture_receipt=receipt,
        pr=18,
        head=current["snapshot_sha"],
        base="main",
        repository="fixture/repo",
    )
    binding = coordinator.seal(binding, authority_kind="coordinator")
    records.append(binding)
    first = provisional.materialize_publication_binding(
        repo, authority_records=records, binding=binding
    )
    assert (
        provisional.materialize_publication_binding(
            repo, authority_records=records, binding=binding
        )
        == first
    )


def resolved(case):
    repo, _, _, records, *_ = case
    return resolved_loopzero_predecessors(
        repo,
        records=records,
        terminals=authority.authenticated_review_terminals(records),
        current_review_task_id="review-2",
        verdicts=authority.authenticated_verdicts(records),
    )


def replace_row(case, row, payload, *, coordinator=False):
    signer = case[1] if coordinator else case[2]
    updated = signer.seal(
        payload, authority_kind="coordinator" if coordinator else "dispatcher"
    )
    case[3][case[3].index(row)] = updated
    return updated


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unsigned",
        "forged",
        "wrong-predecessor",
        "wrong-snapshot",
        "missing-authority",
        "wrong-reason",
        "wrong-source",
        "wrong-state",
        "replayed-supersession",
        "late-supersession",
    ],
)
@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
def test_invalid_supersession_cannot_resolve_or_bind(replacement, damage):
    repo, _, _, records, admission, receipt, _, current = replacement
    row = next(row for row in records if row["type"] == "attempt-supersession")
    payload = unsigned(row)
    if damage == "missing":
        records.remove(row)
    elif damage == "unsigned":
        records[records.index(row)] = payload
    elif damage == "forged":
        records[records.index(row)] = {**row, "superseding_source_identity": {}}
    elif damage == "replayed-supersession":
        records.append(row.copy())
    elif damage == "late-supersession":
        records.remove(row)
        records.append(row)
    else:
        proof = payload["uncarryable_delta_authority"]
        if damage == "wrong-predecessor":
            proof["predecessor_task_id"] = "unrelated"
        elif damage == "wrong-snapshot":
            proof["predecessor_snapshot_sha"] = "c" * 40
        elif damage == "missing-authority":
            payload.pop("uncarryable_delta_authority")
        elif damage == "wrong-reason":
            proof["reason"] = "please-ignore-findings"
        else:
            key = "head" if damage == "wrong-source" else "state_sha256"
            payload["superseding_source_identity"][key] = "c" * len(
                current["source_identity"][key]
            )
        replace_row(replacement, row, payload, coordinator=True)
    assert resolved(replacement) == set()
    # With no supersession the original producer is still standing and bindable;
    # those findings remain blockers. A real superseded producer cannot bind.
    if "review-1" in authority.authenticated_review_terminals(records):
        assert open_important_finding_ids(
            repo,
            "refs/heads/fix/18",
            dispatch_records=records,
            finding_records=[],
            current_review_task_id="review-2",
        )
    else:
        with pytest.raises(provisional.ProvisionalFindingError):
            provisional.build_publication_binding(
                records,
                repo=repo,
                admission=admission,
                capture_receipt=receipt,
                pr=18,
                head=current["snapshot_sha"],
                base="main",
                repository="fixture/repo",
            )


@pytest.mark.parametrize(
    "damage",
    [
        "run",
        "ref",
        "chain",
        "missing-chain",
        "lens",
        "intent",
        "snapshot",
        "state",
        "repository",
        "failed-verdict",
        "unsigned-verdict",
        "forged-terminal",
        "replayed-start",
        "not-full",
    ],
)
@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
def test_replacement_identity_and_replay_boundaries(replacement, damage):
    repo, coordinator, dispatcher, records, admission, receipt, _, current = replacement
    if damage in {"failed-verdict", "unsigned-verdict"}:
        verdict = records[-1]
        payload = unsigned(verdict)
        if damage == "failed-verdict":
            payload["verdict"] = "fail"
            replace_row(replacement, verdict, payload, coordinator=True)
        else:
            records[-1] = payload
    elif damage == "forged-terminal":
        records[records.index(current)] = {**current, "result_sha256": "0" * 64}
    elif damage == "replayed-start":
        start = next(
            row
            for row in records
            if row["type"] == "attempt-start" and row["task_id"] == "review-2"
        )
        earlier = {
            **unsigned(start),
            "task_id": "first-replacement",
            "work_unit_id": "first-replacement",
            "terminal_authority": dispatcher.registration(),
        }
        records.insert(
            records.index(start),
            coordinator.seal(earlier, authority_kind="coordinator"),
        )
    else:
        for row in list(records):
            if row.get("task_id") != "review-2" or row["type"] not in {
                "attempt-start",
                "attempt-terminal",
            }:
                continue
            payload = unsigned(row)
            contract = payload["task_contract"]
            if damage == "run":
                payload["run_id"] = "sr_" + "2" * 32
            elif damage == "ref":
                payload["source_identity"]["ref"] = "refs/heads/other"
            elif damage == "chain":
                contract["review_chain_id"] = "other"
            elif damage == "missing-chain":
                contract.pop("review_chain_id")
            elif damage == "lens":
                contract["required_sections"] = ["code"]
                contract["security_trigger_paths"] = []
            elif damage == "intent":
                contract["review_intent"] = "discovery"
            elif damage == "snapshot":
                payload["snapshot_sha"] = "c" * 40
            elif damage == "state":
                payload["source_identity"]["state_sha256"] = "c" * 64
            elif damage == "repository":
                payload["repository_binding"] = "other"
            elif damage == "not-full":
                contract["delta_from_snapshot"] = "d" * 40
            payload["task_contract_hash"] = gitscope.task_contract_hash(contract)
            if row["type"] == "attempt-start":
                payload["terminal_authority"] = dispatcher.registration()
            replace_row(
                replacement, row, payload, coordinator=row["type"] == "attempt-start"
            )
    assert resolved(replacement) == set()
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_publication_binding(
            records,
            repo=repo,
            admission=admission,
            capture_receipt=receipt,
            pr=18,
            head=current["snapshot_sha"],
            base="main",
            repository="fixture/repo",
        )


def test_current_important_finding_remains_blocking(replacement):
    repo, _, _, records, _, _, prior, _ = replacement
    findings = [
        {
            "finding_id": "old",
            "review_task_id": "review-1",
            "state": "open",
            "severity": "important",
        },
        {
            "finding_id": "new",
            "review_task_id": "review-2",
            "state": "open",
            "severity": "important",
        },
    ]
    assert open_important_finding_ids(
        repo,
        prior["source_identity"]["ref"],
        dispatch_records=records,
        finding_records=findings,
        current_review_task_id="review-2",
    ) == ("new",)


@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
def test_binding_cannot_use_replacement_at_a_different_head(replacement):
    repo, _, _, records, admission, receipt, *_ = replacement
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_publication_binding(
            records,
            repo=repo,
            admission=admission,
            capture_receipt=receipt,
            pr=18,
            head="c" * 40,
            base="main",
            repository="fixture/repo",
        )


@pytest.mark.parametrize(
    "field", ["family", "root_work_unit_id", "delivery_family_id", "slice_id"]
)
@pytest.mark.parametrize("value", ["", "unrelated"])
@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
def test_replacement_family_and_delivery_lineage_are_exact(replacement, field, value):
    current = replacement[-1]
    payload = unsigned(current)
    if field in {"delivery_family_id", "slice_id"}:
        payload["task_contract"][field] = value
        payload["task_contract_hash"] = gitscope.task_contract_hash(
            payload["task_contract"]
        )
    else:
        payload[field] = value
    replace_row(replacement, current, payload)
    assert resolved(replacement) == set()


@pytest.mark.parametrize("value", ["", None])
def test_even_empty_delta_field_is_not_a_full_replacement(replacement, value):
    current = replacement[-1]
    payload = unsigned(current)
    payload["task_contract"]["delta_from_snapshot"] = value
    payload["task_contract_hash"] = gitscope.task_contract_hash(
        payload["task_contract"]
    )
    replace_row(replacement, current, payload)
    assert resolved(replacement) == set()


@pytest.mark.parametrize("state", ["missing", "unrelated", "invalid", "changed"])
@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
def test_missing_or_invalid_run_cannot_resolve_or_bind(replacement, state):
    import json

    repo, _, _, records, admission, receipt, _, current = replacement
    path = repo / ".audit/skill-runs/fixture.jsonl"
    row = json.loads(path.read_text())
    if state == "missing":
        path.unlink()
    elif state == "unrelated":
        path.write_text(json.dumps({**row, "run_id": "sr_" + "3" * 32}) + "\n")
    elif state == "invalid":
        path.write_text(json.dumps({**row, "delivery_contract": "bad"}) + "\n")
    else:
        path.write_text(
            json.dumps(row)
            + "\n"
            + json.dumps({**row, "delivery_contract": "bad"})
            + "\n"
        )
    assert resolved(replacement) == set()
    with pytest.raises((provisional.ProvisionalFindingError, ValueError)):
        provisional.build_publication_binding(
            records,
            repo=repo,
            admission=admission,
            capture_receipt=receipt,
            pr=18,
            head=current["snapshot_sha"],
            base="main",
            repository="fixture/repo",
        )


def test_caller_projections_cannot_grant_full_resolution(replacement):
    repo, _, _, records, _, _, prior, current = replacement
    records[:] = [row for row in records if row["type"] != "attempt-supersession"]
    assert (
        resolved_loopzero_predecessors(
            repo,
            records=records,
            current_review_task_id="review-2",
            terminals={"review-1": prior, "review-2": current},
            verdicts={"review-2": {"verdict": "pass"}},
        )
        == set()
    )


def test_delta_resolution_is_unchanged(replacement):
    repo, _, _, records, _, _, prior, current = replacement
    delta = {
        **current,
        "task_contract": {
            **current["task_contract"],
            "delta_from_snapshot": prior["snapshot_sha"],
        },
    }
    # Preserve the exact legacy consumer call shape without raw records.
    assert resolved_loopzero_predecessors(
        repo,
        terminals={"review-1": prior, "review-2": delta},
        current_review_task_id="review-2",
        verdicts=authority.authenticated_verdicts(records),
    ) == {"review-1"}


def test_no_unrelated_or_recursive_terminal_is_recovered(replacement):
    from loopzero.review.predecessors import publication_predecessor_terminal

    repo, _, _, records, _, _, prior, current = replacement
    assert publication_predecessor_terminal(records, repo, task_id="unrelated") is None
    assert publication_predecessor_terminal(records, repo, task_id="review-2") is None
    assert publication_predecessor_terminal(records, repo, task_id="review-1") is prior


@pytest.mark.parametrize("replacement", [True], indirect=True)
def test_current_provisional_important_finding_is_not_resolved(replacement):
    repo, _, _, records, _, _, prior, _ = replacement
    admission = provisional.authenticated_provisional_admissions(records)["review-2"]
    findings = provisional.authenticated_provisional_findings(
        records, repo, admission=admission
    )
    assert resolved(replacement) == {"review-1"}
    assert open_important_finding_ids(
        repo,
        prior["source_identity"]["ref"],
        dispatch_records=records,
        finding_records=[],
        current_review_task_id="review-2",
    ) == (findings[0]["finding_id"],)


@pytest.mark.parametrize("adopted", [False, True])
@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
@pytest.mark.parametrize("publication_head", ["snapshot", "source"])
def test_actual_rss_callback_requires_authenticated_terminal_mapping(
    replacement, monkeypatch, adopted, publication_head
):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from loopzero.delivery.publish import PublicationError, PublicationRequest
    from loopzero.review.findings import load_finding_records

    repo, coordinator, _, records, admission, _, _, current = replacement
    head = (
        current["source_identity"]["head"]
        if publication_head == "source"
        else current["snapshot_sha"]
    )
    source = (
        Path(__file__).parents[2] / "fixtures/rss_publication_callback.py"
    ).read_text()
    if adopted:
        source = source.replace(
            "from loopzero.delivery._publish_findings import resolved_loopzero_predecessors",
            "from loopzero.review.predecessors import resolved_review_predecessors",
        )
        source = source.replace(
            "selected = resolved_loopzero_predecessors(",
            "resolved = resolved_review_predecessors(",
        )
        source = source.replace(
            "                terminals=terminals,", "                records=records,"
        )
        source = source.replace(
            "                verdicts=host.authenticated_verdicts(records),\n", ""
        )
        source = source.replace(
            "            ) | {review_task_id}",
            "            )\n            terminals.update(resolved)\n            selected = set(resolved) | {review_task_id}",
        )
    monkeypatch.setitem(
        sys.modules,
        "agent_dispatch",
        SimpleNamespace(
            authority_ledger_lock=authority_store.authority_ledger_lock,
            load_authority_records=lambda *args: records,
            WORK_UNIT_HISTORY_DAYS=30,
            authenticated_review_terminals=authority.authenticated_review_terminals,
            authenticated_verdicts=authority.authenticated_verdicts,
            create_coordinator_authority=lambda: coordinator,
            _record_envelope=lambda payload: {
                **payload,
                "schema_version": current["schema_version"],
                "policy_version": current["policy_version"],
            },
            _append_serialized_record_unlocked=lambda repo, row: records.append(row),
        ),
    )
    namespace = {
        "PublicationRequest": PublicationRequest,
        "PublicationError": PublicationError,
    }
    exec(compile(source, "rss_publication_callback.py", "exec"), namespace)
    request = namespace["publication_request"](
        authority_repo=repo,
        run_id=current["run_id"],
        title="RSS",
        body="reviewed",
        head="fix/18",
        base="main",
        expected_head=head,
        review_task_id="review-2",
    )
    request.bind_provisional_findings(18, head, "main", "fixture/repo")
    owner = admission["provisional_owner_id"]
    bindings = provisional.authenticated_publication_bindings(records)
    assert (owner in bindings) is adopted
    materialized = load_finding_records(repo)
    assert bool(materialized) is adopted
    if adopted:
        assert {row["review_task_id"] for row in materialized} == {"review-1"}
        assert all(row["state"] == "open" for row in materialized)
        request.bind_provisional_findings(18, head, "main", "fixture/repo")
        assert load_finding_records(repo) == materialized


def test_real_rss_full_task_derives_original_lineage_without_caller_ids():
    from pathlib import Path

    namespace = {}
    source = (Path(__file__).parents[2] / "fixtures/rss_review_identity.py").read_text()
    exec(compile(source, "rss_review_identity.py", "exec"), namespace)
    run_id = "sr_42ea7fe4f4824b16be2ef09f798122b0"
    task = {
        "objective": "Full RSS publication review after FL-2 supersession",
        "work_unit_id": "rss-publisher-binding-full",
        "size_points": 1,
        "work_kind": "review",
        "category": "delivery-code-review",
        "review_intent": "delivery-code-review",
        "evidence_paths": ["evidence.md"],
    }
    identity = namespace["bind_review_identity"](task, records=[], run_id=run_id)
    assert identity == namespace["review_identity"](run_id, transition_ordinal=0)
    assert identity["delivery_family_id"].startswith("rf_1ebb")
    assert identity["slice_id"].startswith("rs_d3ef")
    assert identity["review_chain_id"].startswith("rc_33ed")
    assert identity["root_work_unit_id"] == identity["review_chain_id"]
    assert "delta_from_snapshot" not in task and "review_lens" not in task
    assert "run_id" not in task and "task_id" not in task


def test_unrelated_malformed_run_does_not_break_publication_scan(
    replacement, monkeypatch
):
    from loopzero.review.predecessors import publication_predecessor_terminal

    repo, _, _, records, _, _, prior, current = replacement
    accepted = authority.accepted_review_terminals(records)
    verdicts = authority.authenticated_verdicts(records)
    malformed = {**current, "task_id": "unrelated"}
    malformed.pop("run_id")
    monkeypatch.setattr(
        authority,
        "accepted_review_terminals",
        lambda records: {**accepted, "unrelated": malformed},
    )
    monkeypatch.setattr(
        authority,
        "authenticated_verdicts",
        lambda records: {**verdicts, "unrelated": {"verdict": "pass"}},
    )
    assert publication_predecessor_terminal(records, repo, task_id="review-1") is prior


@pytest.mark.parametrize("replacement", [False, "synthetic"], indirect=True)
@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "candidate-sha",
        "candidate-tree",
        "snapshot-tree",
        "source-head",
        "missing-patch",
        "forged-head",
        "forged-tree",
        "forged-patch",
    ],
)
def test_current_source_snapshot_patch_binding_fails_closed(replacement, damage):
    records = replacement[3]
    # Accepted uncaptured signed records exercise the resolver guards directly.
    # The separate liveness fixture above retains the real capture path.
    records[:] = [
        row
        for row in records
        if not (
            row.get("task_id") == "review-2"
            and row["type"] == "finding-capture-admission-v1"
        )
    ]
    for row in list(records):
        if row.get("task_id") != "review-2" or row["type"] not in {
            "attempt-start",
            "attempt-terminal",
        }:
            continue
        payload = unsigned(row)
        for key in tuple(payload):
            if key.startswith("finding_capture") or key == "provisional_owner_id":
                payload.pop(key)
        if damage in {"candidate-sha", "forged-patch"}:
            payload["patch_identity"]["candidate_sha"] = "c" * 40
        elif damage == "candidate-tree":
            payload["patch_identity"]["candidate_tree_sha"] = "c" * 40
        elif damage in {"snapshot-tree", "forged-tree"}:
            payload["snapshot_tree_sha"] = "c" * 40
        elif damage in {"source-head", "forged-head"}:
            payload["source_identity"]["head"] = "c" * 40
        elif damage == "missing-patch":
            payload.pop("patch_identity")
        payload["review_chain_receipt"] = chain.build_review_chain_receipt(
            task={**payload["task_contract"], "task_id": "review-2"},
            snapshot_sha=payload["snapshot_sha"],
            snapshot_tree_sha=payload["snapshot_tree_sha"],
            patch_identity=payload.get("patch_identity"),
            result=json.loads(
                (replacement[0] / replacement[-1]["result_artifact"]).read_text()
            ),
            finding_ids=[],
        )
        if damage.startswith("forged-"):
            # Keep the original proof: shape/self-digest cannot grant authority.
            records[records.index(row)] = {**row, **payload}
        else:
            if row["type"] == "attempt-start":
                payload["terminal_authority"] = replacement[2].registration()
            replace_row(
                replacement, row, payload, coordinator=row["type"] == "attempt-start"
            )
    if not damage.startswith("forged-"):
        assert "review-2" in authority.accepted_review_terminals(records)
    assert resolved(replacement) == ({"review-1"} if damage == "none" else set())

    from loopzero.review.predecessors import publication_predecessor_terminal

    assert publication_predecessor_terminal(
        records,
        replacement[0],
        task_id="review-1",
        head=replacement[-1]["source_identity"]["head"],
    ) is (replacement[-2] if damage == "none" else None)
