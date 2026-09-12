import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority as kernel_authority
from loopzero.kernel import authority_projection, authority_store, policy
from loopzero.review import evidence, findings


def _configure(tmp_path):
    profile = SimpleNamespace(
        root=tmp_path,
        audit_root=Path(".audit"),
        finding_severities=("critical", "important", "suggestion"),
    )
    findings.configure(profile)
    evidence.configure(profile)


def _write_run(tmp_path, run_id, *, pr, outcome="merged"):
    runs = tmp_path / ".audit" / "skill-runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-09-12.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "run_id": run_id,
                    "pr": pr,
                    "delivery_contract": "loop-zero-v1",
                    "outcome": "in_progress",
                },
                {
                    "run_id": run_id,
                    "pr": pr,
                    "delivery_contract": "loop-zero-v1",
                    "outcome": outcome,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _install_authenticated_run(monkeypatch, *, run_id, pr, outcome):
    dispatcher = kernel_authority.TerminalAuthority.generate()
    coordinator = kernel_authority.CoordinatorAuthority(
        dispatcher.public_key, dispatcher._private_key
    )
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    common = {
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }
    cutover = coordinator.seal(
        {
            **common,
            "type": "coordinator-authority-cutover",
            "status": "active",
            "ledger_prefix": authority_projection.coordinator_ledger_prefix([]),
        },
        authority_kind="coordinator",
    )
    attempt = {
        **common,
        "task_id": f"delivery-{pr}",
        "attempt_index": 0,
        "run_id": run_id,
        "work_unit_id": f"delivery-{pr}",
        "worktree": "/repo",
        "pr": pr,
    }
    start = coordinator.seal(
        {
            **attempt,
            "type": "attempt-start",
            "registration_authority_version": 1,
            "terminal_authority": dispatcher.registration(),
        },
        authority_kind="coordinator",
    )
    terminal = dispatcher.seal(
        {
            **attempt,
            "type": "attempt-terminal",
            "outcome": outcome,
        },
        authority_kind="dispatcher",
    )
    monkeypatch.setattr(
        authority_store,
        "load_authority_records",
        lambda *_args: [cutover, start, terminal],
    )


def _install_unregistered_terminal(
    monkeypatch, *, run_id, pr, authority_kind="coordinator"
):
    dispatcher = kernel_authority.TerminalAuthority.generate()
    coordinator = kernel_authority.CoordinatorAuthority(
        dispatcher.public_key, dispatcher._private_key
    )
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    common = {
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }
    cutover = coordinator.seal(
        {
            **common,
            "type": "coordinator-authority-cutover",
            "status": "active",
            "ledger_prefix": authority_projection.coordinator_ledger_prefix([]),
        },
        authority_kind="coordinator",
    )
    attempt = {
        **common,
        "task_id": f"delivery-{pr}",
        "attempt_index": 0,
        "run_id": run_id,
        "work_unit_id": f"delivery-{pr}",
        "worktree": "/repo",
        "pr": pr,
        "type": "attempt-terminal",
        "outcome": "merged",
    }
    signer = coordinator if authority_kind == "coordinator" else dispatcher
    terminal = signer.seal(attempt, authority_kind=authority_kind)
    monkeypatch.setattr(
        authority_store,
        "load_authority_records",
        lambda *_args: [cutover, terminal],
    )


def _request(pr, producer="review-1"):
    return findings.build_finding_capture_request(
        pr=pr,
        producer_id=producer,
        producer_skill="review",
        category="code",
        findings=[
            {"severity": "important", "claim": "fix this", "path": "a.py"},
            {"severity": "suggestion", "claim": "rename this", "path": "a.py"},
        ],
    )


def test_findings_are_pr_scoped_and_suggestions_are_not_persisted(tmp_path):
    _configure(tmp_path)
    one = findings.capture_finding_records(tmp_path, request=_request(17))
    two = findings.capture_finding_records(tmp_path, request=_request(18))

    assert one["finding_ids"] != two["finding_ids"]
    assert [row["claim"] for row in findings.load_finding_records(tmp_path, pr=17)] == [
        "fix this"
    ]
    assert findings.load_finding_records(tmp_path, pr=18)[0]["pr"] == 18


def test_capture_replays_and_authenticated_merge_expires_the_pr(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    run_id = "sr_" + "1" * 32
    _write_run(tmp_path, run_id, pr=17)
    _install_authenticated_run(
        monkeypatch, run_id=run_id, pr=17, outcome="merged"
    )
    receipt = evidence.append_finding_records(
        tmp_path,
        task_id="review-1",
        result={"findings": _request(17)["findings"]},
        snapshot=None,
        worktree=tmp_path,
        advisory=False,
        source_head="a" * 40,
        producer_skill="review",
        category="code",
        delivery_run_id=run_id,
        pr=17,
    )
    assert receipt is not None
    assert evidence.append_finding_records(
        tmp_path,
        task_id="review-1",
        result={"findings": _request(17)["findings"]},
        snapshot=None,
        worktree=tmp_path,
        advisory=False,
        source_head="a" * 40,
        producer_skill="review",
        category="code",
        delivery_run_id=run_id,
        pr=17,
    ) == receipt
    assert findings.load_finding_records(tmp_path, pr=17) == []


def test_unsigned_merged_run_log_row_does_not_expire_findings(tmp_path, monkeypatch):
    _configure(tmp_path)
    run_id = "sr_" + "2" * 32
    _write_run(tmp_path, run_id, pr=17)
    _install_authenticated_run(
        monkeypatch, run_id=run_id, pr=17, outcome="in_progress"
    )
    evidence.append_finding_records(
        tmp_path,
        task_id="review-unsigned-merge",
        result={"findings": _request(17)["findings"]},
        snapshot=None,
        worktree=tmp_path,
        advisory=False,
        source_head="b" * 40,
        producer_skill="review",
        category="code",
        delivery_run_id=run_id,
        pr=17,
    )

    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_unregistered_coordinator_terminal_does_not_expire_findings(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    run_id = "sr_" + "3" * 32
    _write_run(tmp_path, run_id, pr=17)
    _install_unregistered_terminal(
        monkeypatch, run_id=run_id, pr=17, authority_kind="coordinator"
    )

    receipt = evidence.append_finding_records(
        tmp_path,
        task_id="review-coordinator-terminal",
        result={"findings": _request(17)["findings"]},
        snapshot=None,
        worktree=tmp_path,
        advisory=False,
        source_head="d" * 40,
        producer_skill="review",
        category="code",
        delivery_run_id=run_id,
        pr=17,
    )

    assert receipt is not None
    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_unregistered_dispatcher_terminal_does_not_expire_findings(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    run_id = "sr_" + "4" * 32
    _write_run(tmp_path, run_id, pr=17)
    _install_authenticated_run(
        monkeypatch, run_id=run_id, pr=17, outcome="in_progress"
    )
    evidence.append_finding_records(
        tmp_path,
        task_id="review-unregistered-dispatcher",
        result={"findings": _request(17)["findings"]},
        snapshot=None,
        worktree=tmp_path,
        advisory=False,
        source_head="e" * 40,
        producer_skill="review",
        category="code",
        delivery_run_id=run_id,
        pr=17,
    )
    _install_unregistered_terminal(
        monkeypatch, run_id=run_id, pr=17, authority_kind="dispatcher"
    )

    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_delivery_run_for_another_pr_is_rejected_and_keeps_finding_live(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    findings.capture_finding_records(tmp_path, request=_request(17))
    run_id = "sr_" + "9" * 32
    _write_run(tmp_path, run_id, pr=99)
    _install_authenticated_run(
        monkeypatch, run_id=run_id, pr=99, outcome="merged"
    )

    with pytest.raises(findings.LedgerConflict, match="PR 99, not PR 17"):
        evidence.append_finding_records(
            tmp_path,
            task_id="review-cross-pr",
            result={"findings": _request(17)["findings"]},
            snapshot=None,
            worktree=tmp_path,
            advisory=False,
            source_head="c" * 40,
            producer_skill="review",
            category="code",
            delivery_run_id=run_id,
            pr=17,
        )

    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_fake_merge_sha_has_no_expiry_api(tmp_path):
    _configure(tmp_path)
    findings.capture_finding_records(tmp_path, request=_request(17))
    finding_log = tmp_path / ".audit" / "findings" / "2026-09-12.jsonl"
    finding_log.write_text(
        finding_log.read_text(encoding="utf-8")
        + json.dumps({"type": "pr-merged", "pr": 17, "merge_sha": "a" * 40})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AttributeError):
        getattr(findings, "expire_at_merge")(tmp_path, pr=17, merge_sha="a" * 40)
    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_fake_authority_string_cannot_fabricate_a_waiver(tmp_path):
    _configure(tmp_path)
    receipt = findings.capture_finding_records(tmp_path, request=_request(17))
    finding_id = receipt["finding_ids"][0]
    with pytest.raises(TypeError, match="unexpected keyword argument 'pr'"):
        findings.append_finding_transition(
            tmp_path, pr=17, finding_id=finding_id, status="waived",
            authority="maintainer", rationale="accepted risk",
        )
    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_pr_number_is_mandatory(tmp_path):
    _configure(tmp_path)
    with pytest.raises(findings.LedgerConflict, match="positive PR"):
        findings.build_finding_capture_request(
            producer_id="review", findings=[{"severity": "important", "claim": "x"}]
        )


def _install_registration_probe(
    monkeypatch,
    *,
    run_id,
    registration_pr,
    terminal_pr,
    terminal_task_id=None,
    proofless_registration=False,
):
    """Port of the a4v5 review probe: registration and terminal may disagree."""
    dispatcher = kernel_authority.TerminalAuthority.generate()
    coordinator = kernel_authority.CoordinatorAuthority(
        dispatcher.public_key, dispatcher._private_key
    )
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    common = {
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }
    cutover = coordinator.seal(
        {
            **common,
            "type": "coordinator-authority-cutover",
            "status": "active",
            "ledger_prefix": authority_projection.coordinator_ledger_prefix([]),
        },
        authority_kind="coordinator",
    )
    binding = coordinator.seal(
        {
            **common,
            "task_id": "binding-17",
            "attempt_index": 0,
            "run_id": run_id,
            "work_unit_id": "binding-17",
            "worktree": "/repo",
            "pr": 17,
            "type": "attempt-terminal",
            "outcome": "in_progress",
        },
        authority_kind="coordinator",
    )
    holder = [[cutover, binding]]
    monkeypatch.setattr(
        authority_store, "load_authority_records", lambda *_args: holder[0]
    )
    base = {
        **common,
        "task_id": "delivery",
        "attempt_index": 0,
        "run_id": run_id,
        "work_unit_id": "delivery",
        "worktree": "/repo",
    }

    def register_and_settle():
        registration = {
            **base,
            "pr": registration_pr,
            "type": "attempt-start",
            "registration_authority_version": 1,
            "terminal_authority": dispatcher.registration(),
        }
        if proofless_registration:
            # Legacy shape: no terminal_authority_proof at all.
            start = dict(registration)
        else:
            start = coordinator.seal(registration, authority_kind="coordinator")
        terminal_identity = dict(base)
        if terminal_task_id is not None:
            terminal_identity["task_id"] = terminal_task_id
            terminal_identity["work_unit_id"] = terminal_task_id
        terminal = dispatcher.seal(
            {
                **terminal_identity,
                "pr": terminal_pr,
                "type": "attempt-terminal",
                "outcome": "merged",
            },
            authority_kind="dispatcher",
        )
        holder[0] = [cutover, binding, start, terminal]
        return start, terminal

    return register_and_settle


def _probe_finding(tmp_path, *, run_id):
    evidence.append_finding_records(
        tmp_path,
        task_id="review",
        result={
            "findings": [
                {"severity": "critical", "claim": "probe", "path": "a.py"}
            ]
        },
        snapshot=None,
        worktree=tmp_path,
        advisory=False,
        source_head="a" * 40,
        producer_skill="review",
        category="code",
        delivery_run_id=run_id,
        pr=17,
    )


def test_registered_dispatcher_cannot_expire_a_pr_its_registration_never_bound(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    run_id = "sr_" + "c" * 32
    _write_run(tmp_path, run_id, pr=17, outcome="in_progress")
    register_and_settle = _install_registration_probe(
        monkeypatch, run_id=run_id, registration_pr=99, terminal_pr=17
    )
    _probe_finding(tmp_path, run_id=run_id)
    start, terminal = register_and_settle()

    assert start["pr"] == 99 and terminal["pr"] == 17
    assert start["run_id"] == terminal["run_id"]
    assert findings._registered_dispatcher_delivery_terminals(tmp_path) == []
    assert len(findings.load_finding_history(tmp_path, pr=17)) == 2
    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_registered_dispatcher_terminal_bound_to_registration_pr_expires(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    run_id = "sr_" + "c" * 32
    _write_run(tmp_path, run_id, pr=17, outcome="in_progress")
    register_and_settle = _install_registration_probe(
        monkeypatch, run_id=run_id, registration_pr=17, terminal_pr=17
    )
    _probe_finding(tmp_path, run_id=run_id)
    register_and_settle()

    projected = findings._registered_dispatcher_delivery_terminals(tmp_path)
    assert [(row["run_id"], row["pr"], row["outcome"]) for row in projected] == [
        (run_id, 17, "merged")
    ]
    assert len(findings.load_finding_history(tmp_path, pr=17)) == 2
    assert findings.count_live_important(tmp_path, pr=17) == 0


def test_registered_dispatcher_terminal_for_another_attempt_in_same_run_is_ignored(
    tmp_path, monkeypatch
):
    _configure(tmp_path)
    run_id = "sr_" + "c" * 32
    _write_run(tmp_path, run_id, pr=17, outcome="in_progress")
    register_and_settle = _install_registration_probe(
        monkeypatch,
        run_id=run_id,
        registration_pr=17,
        terminal_pr=17,
        terminal_task_id="delivery-other",
    )
    _probe_finding(tmp_path, run_id=run_id)
    start, terminal = register_and_settle()

    assert start["run_id"] == terminal["run_id"]
    assert start["task_id"] != terminal["task_id"]
    assert findings._registered_dispatcher_delivery_terminals(tmp_path) == []
    assert findings.count_live_important(tmp_path, pr=17) == 1


def test_proofless_registration_after_cutover_does_not_expire_findings(
    tmp_path, monkeypatch
):
    """Kernel legacy compatibility stops at the cutover record.

    A proofless attempt-start is accepted as a registration only inside the
    pre-cutover ledger prefix.  Placed after the cutover record it registers
    nothing, so the dispatcher-signed merged terminal it would vouch for
    cannot expire the finding.
    """
    _configure(tmp_path)
    run_id = "sr_" + "c" * 32
    _write_run(tmp_path, run_id, pr=17, outcome="in_progress")
    register_and_settle = _install_registration_probe(
        monkeypatch,
        run_id=run_id,
        registration_pr=17,
        terminal_pr=17,
        proofless_registration=True,
    )
    _probe_finding(tmp_path, run_id=run_id)
    start, terminal = register_and_settle()

    assert "terminal_authority_proof" not in start
    assert terminal["terminal_authority_proof"]["authority_kind"] == "dispatcher"
    records = authority_store.load_authority_records(tmp_path)
    cutover_index = next(
        index
        for index, record in enumerate(records)
        if record.get("type") == "coordinator-authority-cutover"
    )
    assert records.index(start) > cutover_index
    assert findings._registered_dispatcher_delivery_terminals(tmp_path) == []
    assert len(findings.load_finding_history(tmp_path, pr=17)) == 2
    assert findings.count_live_important(tmp_path, pr=17) == 1
