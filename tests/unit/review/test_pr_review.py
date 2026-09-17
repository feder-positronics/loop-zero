"""PR review result contracts, without mutable finding-ledger authority."""

import copy

import pytest

from loopzero.kernel.gitscope import DispatchError
from loopzero.review import pr_review
from loopzero.review.authority import normalize_review_result
from loopzero.runners.contract import governed_result_schema

DIGEST = "a" * 64
FINDING = {"severity": "important", "claim": "Broken behavior", "path": "app.py"}


def task():
    return {
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "primary_result_sha256": DIGEST,
        "required_finding_ids": ["f_" + "b" * 20],
    }


def result(outcome="repaired"):
    return {
        "findings": [],
        "primary_result_sha256": DIGEST,
        "dispositions": [
            {
                "finding_id": "f_" + "b" * 20,
                "outcome": outcome,
                "rationale": "Checked exact repair.",
            }
        ],
    }


@pytest.mark.parametrize("outcome", ["repaired", "rejected", "unresolved"])
def test_delta_contract_accepts_exact_explicit_disposition(outcome):
    assert normalize_review_result(result(outcome), task=task()) == result(outcome)


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "unknown", "digest", "waiver", "blank"]
)
def test_delta_contract_refuses_incomplete_or_invented_authority(damage):
    value = result()
    if damage == "missing":
        value.pop("dispositions")
    if damage == "duplicate":
        value["dispositions"] *= 2
    if damage == "unknown":
        value["dispositions"][0]["finding_id"] = "f_" + "c" * 20
    if damage == "digest":
        value["primary_result_sha256"] = "c" * 64
    if damage == "waiver":
        value["dispositions"][0]["outcome"] = "waived"
    if damage == "blank":
        value["dispositions"][0]["rationale"] = " "
    with pytest.raises(DispatchError):
        normalize_review_result(value, task=task())


def test_clean_delta_does_not_implicitly_close_primary():
    with pytest.raises(DispatchError):
        normalize_review_result({"findings": []}, task=task())


def test_delta_schema_exposes_required_bounded_dispositions():
    schema = governed_result_schema("delta", task=task())
    assert {"dispositions", "primary_result_sha256"} <= set(schema["required"])
    assert schema["properties"]["dispositions"]["maxItems"] == 1


def test_old_review_schema_and_normalization_unchanged():
    legacy = {"work_kind": "review", "review_intent": "delivery-code-review"}
    assert normalize_review_result({"findings": []}, task=legacy) == {"findings": []}
    assert (
        "dispositions" not in governed_result_schema("old", task=legacy)["properties"]
    )


def test_stable_material_ids_exclude_suggestions_and_deduplicate():
    rows = [
        FINDING,
        copy.deepcopy(FINDING),
        {"severity": "suggestion", "claim": "Maybe"},
    ]
    actual = pr_review.material_findings("a" * 40, {"findings": rows})
    assert len(actual) == 1
    assert next(iter(actual)).startswith("f_")


@pytest.fixture
def signed_result(tmp_path, monkeypatch, isolated_ptrace_scope_path):
    import hashlib
    import json

    from loopzero.kernel import (
        authority as signing,
    )
    from loopzero.kernel import (
        authority_projection as projection,
    )
    from loopzero.kernel import (
        authority_store,
    )
    from loopzero.review.acceptance import task_contract_hash

    monkeypatch.setattr(signing, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(
        signing, "_coordinator_state_directory", lambda: tmp_path / "keys"
    )
    coordinator = authority_store.create_coordinator_authority()
    dispatcher = signing.TerminalAuthority.generate()
    identity = {
        "repository": "org/repo",
        "pr": 7,
        "run_id": "sr_" + "1" * 32,
        "head": "a" * 40,
        "branch": "feature/x",
        "base": "main",
        "base_sha": "b" * 40,
    }
    contract = {
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "pr_identity": identity,
    }
    value = {"findings": [FINDING]}
    artifact = tmp_path / "result.json"
    artifact.write_text(json.dumps(value))
    common = {
        "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
        "policy_version": next(iter(projection.COMPATIBLE_DISPATCH_POLICY_VERSIONS)),
        "task_id": "review",
        "work_unit_id": "unit",
        "run_id": identity["run_id"],
        "attempt_index": 0,
        "worktree": str(tmp_path),
        "read_only": True,
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "task_contract": contract,
        "task_contract_hash": task_contract_hash(contract),
        "source_identity": {"head": identity["head"]},
        "snapshot_tree_sha": "c" * 40,
        "result_artifact": "result.json",
        "result_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    rows = [
        coordinator.seal(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": projection.coordinator_ledger_prefix([]),
            },
            authority_kind="coordinator",
            include_public_key=True,
        )
    ]
    rows.append(
        coordinator.seal(
            {
                **common,
                "type": "attempt-start",
                "registration_authority_version": 1,
                "terminal_authority": dispatcher.registration(),
            },
            authority_kind="coordinator",
        )
    )
    rows.append(
        dispatcher.seal(
            {**common, "type": "attempt-terminal", "status": "completed"},
            authority_kind="dispatcher",
        )
    )
    return tmp_path, rows, identity


def test_native_signed_result_binds_registered_pr_and_result_bytes(signed_result):
    repo, rows, identity = signed_result
    from loopzero.kernel import authority_projection as projection
    from loopzero.review.authority import review_terminal_acceptance_reasons

    assert id(rows[-1]) in projection._authenticated_attempt_terminal_ids(rows), (
        projection._authenticated_attempt_terminal_registrations(rows),
        review_terminal_acceptance_reasons(rows[-1]),
    )
    terminal, value = pr_review.authenticated_result(
        repo, rows, "review", pr_identity=identity
    )
    assert value["findings"] == [FINDING]
    obligations = pr_review.delta_obligations(
        repo, rows, "review", pr_identity=identity
    )
    assert obligations["primary_result_sha256"] == terminal["result_sha256"]
    assert len(obligations["required_finding_ids"]) == 1


@pytest.mark.parametrize("damage", ["signature", "registration", "pr", "bytes"])
def test_native_result_rejects_forgery_or_foreign_source(signed_result, damage):
    repo, rows, identity = signed_result
    if damage == "signature":
        rows[-1]["status"] = "failed"
    if damage == "registration":
        rows.pop(1)
    if damage == "pr":
        identity = {**identity, "pr": 8}
    if damage == "bytes":
        (repo / "result.json").write_text('{"findings": []}')
    with pytest.raises(DispatchError):
        pr_review.authenticated_result(repo, rows, "review", pr_identity=identity)


class GitHub:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def json(self, argv):
        self.calls.append(argv)
        if argv[-1] == "repos/{owner}/{repo}":
            return {"full_name": "org/repo"}
        return self.payload


@pytest.mark.parametrize(
    "damage", [None, "head", "base", "owner", "run", "closed", "bool"]
)
def test_live_pr_registration_refuses_foreign_or_changed_identity(
    signed_result, damage
):
    _, _, identity = signed_result
    payload = {
        "number": 7,
        "state": "open",
        "body": "<!-- skill-run-id: " + identity["run_id"] + " -->",
        "head": {
            "sha": identity["head"],
            "ref": identity["branch"],
            "repo": {"full_name": "org/repo"},
        },
        "base": {
            "sha": identity["base_sha"],
            "ref": "main",
            "repo": {"full_name": "org/repo"},
        },
    }
    if damage == "head":
        payload["head"]["sha"] = "d" * 40
    if damage == "base":
        payload["base"]["sha"] = "d" * 40
    if damage == "owner":
        payload["head"]["repo"]["full_name"] = "other/repo"
    if damage == "run":
        payload["body"] = ""
    if damage == "closed":
        payload["state"] = "closed"
    if damage == "bool":
        identity = {**identity, "pr": True}
    if damage:
        with pytest.raises(DispatchError):
            pr_review.verify_pr_identity(GitHub(payload), **identity)
    else:
        assert pr_review.verify_pr_identity(GitHub(payload), **identity) == identity


@pytest.mark.parametrize(
    "damage", [None, "edited", "pending", "deleted", "wrong-head", "unpaged"]
)
def test_projection_requires_exact_published_body_and_complete_pages(
    signed_result, damage
):
    repo, rows, identity = signed_result
    body = pr_review.projection_body(repo, rows, "review", pr_identity=identity)
    value = {"body": body, "commit_id": identity["head"], "state": "COMMENTED"}
    if damage == "edited":
        value["body"] = body.split("\n")[0]
    if damage == "pending":
        value["state"] = "PENDING"
    if damage == "wrong-head":
        value["commit_id"] = "e" * 40
    pages = [[], [value]]
    if damage == "deleted":
        pages = [[]]
    if damage == "unpaged":
        pages = {"reviews": [value]}
    runner = GitHub(pages)
    if damage:
        with pytest.raises(DispatchError):
            pr_review.require_projection(runner, pr_identity=identity, body=body)
    else:
        pr_review.require_projection(runner, pr_identity=identity, body=body)
        assert "--paginate" in runner.calls[0] and "--slurp" in runner.calls[0]


def test_v2_admission_requires_live_pr_before_any_review_slot(consumer):
    import json
    import subprocess

    from loopzero.kernel import authority_store, worktree_lease
    from loopzero.review import admission

    head = subprocess.check_output(
        ["git", "-C", str(consumer), "rev-parse", "HEAD"], text=True
    ).strip()
    run = "sr_" + "2" * 32
    path = consumer / ".audit/skill-runs/runs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run,
                "delivery_contract": "loop-zero-v2",
                "git_branch": "main",
                "outcome": "in_progress",
            }
        )
        + "\n"
    )
    identity = {"candidate_tree_sha": "c" * 40, "candidate_sha": head}
    with authority_store.authority_ledger_lock(consumer):
        result = admission.admit_review(
            consumer,
            [],
            repository_binding="repo",
            task={
                "task_id": "review",
                "idempotency_key": "review",
                "run_id": run,
                "review_intent": "delivery-code-review",
            },
            current_source_identity=worktree_lease.source_identity(consumer),
            current_tree_sha=identity["candidate_tree_sha"],
            patch_identity=identity,
            required_sections=("code",),
            equivalence_proof=None,
            format_only_proof=None,
            requested="review",
            changed_paths=None,
            security_trigger_paths=(),
        )
    assert isinstance(result, admission.Blocked)
    assert not result.records_to_append
    assert result.code == "missing-evidence"


def test_zero_primary_blockers_require_explicit_empty_delta_dispositions():
    empty_task = {**task(), "required_finding_ids": []}
    value = {"findings": [], "primary_result_sha256": DIGEST, "dispositions": []}
    assert normalize_review_result(value, task=empty_task) == value
    schema = governed_result_schema("delta", task=empty_task)
    assert schema["properties"]["dispositions"]["maxItems"] == 0
    assert (
        "enum"
        not in schema["properties"]["dispositions"]["items"]["properties"]["finding_id"]
    )
