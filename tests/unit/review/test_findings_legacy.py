import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


from loopzero.review import findings as ledger


@pytest.fixture(autouse=True)
def _consumer_campaign_metrics_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep mixed lifecycle tests intact while campaign metrics stay consumer-side."""
    monkeypatch.setattr(ledger, "_CONFIGURED_ROOT", None)
    monkeypatch.setattr(ledger, "_REQUIRE_PR_SCOPE", False)
    monkeypatch.setattr(ledger, "FINDINGS_DIR", Path(".audit/findings"))
    monkeypatch.setattr(ledger, "OPERATIONS_DIR", Path(".audit/finding-operations"))
    monkeypatch.setattr(
        ledger,
        "_SEVERITY_RANK",
        {"suggestion": 1, "important": 2, "critical": 3},
    )
    if hasattr(ledger, "compute_metrics"):
        return

    def governed_read(repo: Path, *args, **kwargs):
        del args, kwargs
        primary = ledger._primary(repo, require_git_primary=True)
        ledger.load_finding_history(primary, strict_malformed=True)
        return {}

    monkeypatch.setattr(ledger, "compute_metrics", governed_read, raising=False)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-qm", "fixture")
    return path


def write_records(repo: Path, *records: dict[str, object]) -> None:
    directory = repo / ".audit" / "findings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-08-07.jsonl"
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def finding(
    finding_id: str = "f_one",
    *,
    severity: str = "important",
    state: str = "open",
    disposition: str | None = None,
    ts: str = "2026-08-01T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "state": state,
        "disposition": disposition,
        "claim": f"claim for {finding_id}",
        "review_task_id": "review-one",
        "ts": ts,
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def owner(worker: str = "worker-a") -> dict[str, str]:
    return {
        "coordinator_id": "coordinator-3266",
        "run_id": "run-3284",
        "worker_session_id": worker,
    }


def test_primary_root_and_strict_reader_ignore_worktree_local_ledger(
    repo: Path, tmp_path: Path
) -> None:
    write_records(repo, finding())
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "fixture-linked", str(worktree))
    write_records(worktree, finding("f_shadow", severity="critical"))

    assert ledger.primary_repo_root(worktree) == repo
    records = ledger.load_finding_records(worktree, require_existing_for_discovery=True)

    assert [record["finding_id"] for record in records] == ["f_one"]


def test_mutations_and_mutation_relevant_metrics_require_git_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    first = finding()
    second = finding("f_two")
    write_records(checkout, first, second)
    monkeypatch.setattr(
        ledger,
        "_run_git",
        lambda *_args: (_ for _ in ()).throw(ledger.LedgerReadError("git unavailable")),
    )

    assert [
        record["finding_id"] for record in ledger.load_finding_records(checkout)
    ] == [
        "f_one",
        "f_two",
    ]
    recovery_proof = {
        "owner_gone": True,
        "evidence": [
            {"source": "worktree_guard", "status": "gone", "reference": "guard"},
            {"source": "delivery_liveness", "status": "gone", "reference": "run"},
            {"source": "worker_session", "status": "gone", "reference": "session"},
        ],
    }
    actions = (
        lambda: ledger.append_finding_transition(
            checkout,
            "f_one",
            {"disposition": "candidate-resolved"},
            operation_id="transition-no-git",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(first),
        ),
        lambda: ledger.acquire_finding_lease(
            checkout,
            finding_ids=["f_one"],
            coordinator_id="coordinator-3266",
            run_id="run-3284",
            worker_session_id="worker-a",
            worktree="/tmp/worker-a",
            branch="fix/a",
            starting_head="a" * 40,
            work_kind="evidence-only",
            operation_id="lease-no-git",
        ),
        lambda: ledger.release_finding_lease(
            checkout,
            lease_id="fl_missing",
            finding_ids=["f_one"],
            operation_id="release-no-git",
            lease_owner=owner(),
            reason="release must not use local authority",
        ),
        lambda: ledger.mark_finding_lease_owner_terminal(
            checkout,
            lease_id="fl_missing",
            finding_ids=["f_one"],
            operation_id="terminal-lease-no-git",
            terminal_evidence={"terminal": True},
        ),
        lambda: ledger.recover_finding_lease(
            checkout,
            lease_id="fl_missing",
            finding_ids=["f_one"],
            operation_id="recover-lease-no-git",
            recovery_proof=recovery_proof,
        ),
        lambda: ledger.record_terminal_handoff(
            checkout,
            operation_id="handoff-no-git",
            mode="evidence-only",
            finding_ids=["f_one"],
            terminal=False,
            terminal_session_id="worker-a",
            terminal_run_id="run-3284",
            branch="fix/a",
            starting_head="a" * 40,
            final_head="a" * 40,
            worktree="/tmp/worker-a",
            worktree_clean=True,
            commits=[],
            pr_number=None,
            merge_commit=None,
            primary_contains_merge=False,
            validations=[],
            accepted_review_evidence=[],
            unresolved_findings=[],
            blockers=[],
        ),
        lambda: ledger.append_reviewed_lineage(
            checkout,
            finding_ids=["f_one", "f_two"],
            review_task_id="review-one",
            accepted_review_evidence={"task_id": "review-one", "accepted": True},
            operation_id="lineage-no-git",
        ),
        lambda: ledger.append_severity_correction(
            checkout,
            finding_id="f_one",
            new_severity="suggestion",
            reason="reassessed",
            decision_record="docs/decisions.md#no-git",
            reviewer_task_id="review-one",
            owner_approved=False,
            operation_id="severity-no-git",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(first),
        ),
        lambda: ledger.compute_metrics(checkout),
    )

    for action in actions:
        with pytest.raises(ledger.LedgerReadError, match="git unavailable"):
            action()


def test_strict_discovery_reader_rejects_missing_and_malformed_ledgers(
    repo: Path,
) -> None:
    with pytest.raises(ledger.LedgerReadError, match="does not exist"):
        ledger.load_finding_records(repo, require_existing_for_discovery=True)

    directory = repo / ".audit" / "findings"
    directory.mkdir(parents=True)
    (directory / "2026-08-07.jsonl").write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ledger.LedgerReadError, match="invalid JSON"):
        ledger.count_live_important(repo, require_existing_for_discovery=True)


def test_latest_wins_count_and_digest_cover_the_complete_record(repo: Path) -> None:
    initial = finding()
    addressed = {**initial, "state": "addressed", "ts": "2026-08-02T00:00:00+00:00"}
    candidate = finding("f_two", disposition="candidate-resolved")
    write_records(
        repo, initial, addressed, candidate, finding("f_low", severity="suggestion")
    )

    assert ledger.count_live_important(repo) == 1
    assert ledger.canonical_record_digest(initial) != ledger.canonical_record_digest(
        {**initial, "ts": "2026-08-01T00:00:01+00:00"}
    )


def test_guarded_transition_rejects_stale_preconditions_and_is_idempotent(
    repo: Path,
) -> None:
    current = finding()
    write_records(repo, current)
    digest = ledger.canonical_record_digest(current)

    transitioned = ledger.append_finding_transition(
        repo,
        "f_one",
        {"disposition": "candidate-resolved"},
        operation_id="transition-one",
        expected_state="open",
        expected_disposition=None,
        expected_digest=digest,
    )
    replay = ledger.append_finding_transition(
        repo,
        "f_one",
        {"disposition": "candidate-resolved"},
        operation_id="transition-one",
        expected_state="open",
        expected_disposition=None,
        expected_digest=digest,
    )

    assert replay == transitioned
    assert len(ledger.load_finding_history(repo)) == 2
    with pytest.raises(ledger.LedgerConflict, match="different payload"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"state": "addressed"},
            operation_id="transition-one",
            expected_state="open",
            expected_disposition=None,
            expected_digest=digest,
        )
    with pytest.raises(ledger.LedgerConflict, match="expected digest"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"state": "addressed"},
            operation_id="transition-two",
            expected_state="open",
            expected_disposition="candidate-resolved",
            expected_digest=digest,
        )


def test_concurrent_identical_transition_retries_append_once(repo: Path) -> None:
    current = finding()
    write_records(repo, current)
    digest = ledger.canonical_record_digest(current)

    def transition() -> dict[str, object]:
        return ledger.append_finding_transition(
            repo,
            "f_one",
            {"disposition": "candidate-resolved"},
            operation_id="concurrent-transition",
            expected_state="open",
            expected_disposition=None,
            expected_digest=digest,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: transition(), range(16)))

    assert all(result == results[0] for result in results)
    assert len(ledger.load_finding_history(repo)) == 2


def test_single_owner_lease_rejects_visible_lease_id_and_requires_exact_owner(
    repo: Path,
) -> None:
    current = finding()
    write_records(repo, current)
    lease = ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="implementation",
        operation_id="lease-one",
    )

    assert ledger.active_lease(repo, "f_one")["lease_id"] == lease["lease_id"]
    assert (
        ledger.acquire_finding_lease(
            repo,
            finding_ids=["f_one"],
            coordinator_id="coordinator-3266",
            run_id="run-3284",
            worker_session_id="worker-a",
            worktree="/tmp/worker-a",
            branch="fix/a",
            starting_head="a" * 40,
            work_kind="implementation",
            operation_id="lease-one",
        )
        == lease
    )
    with pytest.raises(ledger.LedgerLeaseConflict):
        ledger.acquire_finding_lease(
            repo,
            finding_ids=["f_one"],
            coordinator_id="coordinator-3266",
            run_id="run-3284",
            worker_session_id="worker-b",
            worktree="/tmp/worker-b",
            branch="fix/b",
            starting_head="b" * 40,
            work_kind="evidence-only",
            operation_id="lease-two",
        )
    with pytest.raises(ledger.LedgerLeaseConflict, match="active lease"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"state": "addressed"},
            operation_id="transition-one",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
            lease_owner=owner("worker-b"),
        )
    with pytest.raises(ledger.LedgerAuthorityError, match="full owner Mapping"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"state": "addressed"},
            operation_id="transition-visible-lease-id",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
            lease_owner=str(lease["lease_id"]),
        )

    transitioned = ledger.append_finding_transition(
        repo,
        "f_one",
        {
            "state": "addressed",
            # P4 provenance-at-write: a terminal transition carries its
            # binding in the same operation, never as a follow-up repair.
            "disposition": "review-confirmed-addressed",
            "fix_commit": "d" * 40,
        },
        operation_id="transition-exact-owner",
        expected_state="open",
        expected_disposition=None,
        expected_digest=ledger.canonical_record_digest(current),
        lease_owner=owner(),
    )

    assert transitioned["state"] == "addressed"


def test_promotion_reopen_bypasses_lease_but_invalidates_worker_digest(
    repo: Path,
) -> None:
    closed = {
        **finding(state="addressed", severity="suggestion"),
        "advisory": True,
    }
    write_records(repo, closed)
    ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="implementation",
        operation_id="lease-one",
    )
    old_digest = ledger.canonical_record_digest(closed)

    reopened = ledger.append_finding_transition(
        repo,
        "f_one",
        {
            "state": "open",
            "advisory": None,
            "disposition": None,
            "severity": "critical",
        },
        operation_id="promotion-reopen",
        expected_state="addressed",
        expected_disposition=None,
        expected_digest=old_digest,
        allow_promotion_reopen=True,
    )

    assert reopened["state"] == "open"
    assert reopened["severity"] == "critical"
    assert ledger.active_lease(repo, "f_one") is not None
    with pytest.raises(ledger.LedgerConflict, match="expected state|expected digest"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"state": "addressed"},
            operation_id="worker-stale-transition",
            expected_state="addressed",
            expected_disposition=None,
            expected_digest=old_digest,
            lease_owner=owner(),
        )


def test_promotion_reopen_flag_cannot_bypass_lease_for_an_arbitrary_close(
    repo: Path,
) -> None:
    current = {**finding(), "advisory": True}
    write_records(repo, current)
    ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="implementation",
        operation_id="lease-one",
    )

    with pytest.raises(ledger.LedgerAuthorityError, match="advisory-to-formal"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"state": "addressed"},
            operation_id="malicious-bypass",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
            allow_promotion_reopen=True,
        )

    with pytest.raises(ledger.LedgerAuthorityError, match="cannot lower severity"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {
                "state": "open",
                "advisory": None,
                "disposition": None,
                "severity": "suggestion",
            },
            operation_id="malicious-promotion-demotion",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
            allow_promotion_reopen=True,
        )


def test_evidence_only_handoff_cannot_settle_implementation_lease(repo: Path) -> None:
    write_records(repo, finding())
    ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="implementation",
        operation_id="lease-one",
    )

    with pytest.raises(ledger.LedgerAuthorityError, match="evidence-only"):
        ledger.record_terminal_handoff(
            repo,
            operation_id="handoff-one",
            mode="evidence-only",
            finding_ids=["f_one"],
            terminal=True,
            terminal_session_id="worker-a",
            terminal_run_id="run-3284",
            branch="fix/a",
            starting_head="a" * 40,
            final_head="b" * 40,
            worktree="/tmp/worker-a",
            worktree_clean=True,
            commits=[],
            pr_number=None,
            merge_commit=None,
            primary_contains_merge=False,
            validations=[],
            accepted_review_evidence=[],
            unresolved_findings=[],
            blockers=[],
            lease_owner=owner(),
            settle_leases=True,
        )
    assert ledger.active_lease(repo, "f_one") is not None


def test_terminal_handoff_rejects_bare_lease_id_as_settlement_authority(
    repo: Path,
) -> None:
    write_records(repo, finding())
    lease = ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="implementation",
        operation_id="lease-one",
    )

    with pytest.raises(ledger.LedgerAuthorityError, match="full owner Mapping"):
        ledger.record_terminal_handoff(
            repo,
            operation_id="handoff-bare-lease-id",
            mode="implementation",
            finding_ids=["f_one"],
            terminal=True,
            terminal_session_id="worker-a",
            terminal_run_id="run-3284",
            branch="fix/a",
            starting_head="a" * 40,
            final_head="b" * 40,
            worktree="/tmp/worker-a",
            worktree_clean=True,
            commits=["b" * 40],
            pr_number=123,
            merge_commit="c" * 40,
            primary_contains_merge=True,
            validations=[{"command": "pytest", "passed": True}],
            accepted_review_evidence=[{"task_id": "review-pass", "accepted": True}],
            unresolved_findings=[],
            blockers=[],
            lease_owner=str(lease["lease_id"]),
            settle_leases=True,
        )

    assert ledger.active_lease(repo, "f_one") is not None


def test_successful_implementation_handoff_requires_merged_accepted_evidence(
    repo: Path,
) -> None:
    write_records(repo, finding())
    ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="implementation",
        operation_id="lease-one",
    )

    handoff = ledger.record_terminal_handoff(
        repo,
        operation_id="handoff-one",
        mode="implementation",
        finding_ids=["f_one"],
        terminal=True,
        terminal_session_id="worker-a",
        terminal_run_id="run-3284",
        branch="fix/a",
        starting_head="a" * 40,
        final_head="b" * 40,
        worktree="/tmp/worker-a",
        worktree_clean=True,
        commits=["b" * 40],
        pr_number=123,
        merge_commit="c" * 40,
        primary_contains_merge=True,
        validations=[{"command": "pytest", "passed": True}],
        accepted_review_evidence=[{"task_id": "review-pass", "accepted": True}],
        unresolved_findings=[],
        blockers=[],
        lease_owner=owner(),
        settle_leases=True,
    )

    assert handoff["settled_lease_ids"]
    assert ledger.active_lease(repo, "f_one") is None


def test_release_and_recovery_require_owner_or_audited_owner_gone_proof(
    repo: Path,
) -> None:
    write_records(repo, finding())
    lease = ledger.acquire_finding_lease(
        repo,
        finding_ids=["f_one"],
        coordinator_id="coordinator-3266",
        run_id="run-3284",
        worker_session_id="worker-a",
        worktree="/tmp/worker-a",
        branch="fix/a",
        starting_head="a" * 40,
        work_kind="evidence-only",
        operation_id="lease-one",
    )
    with pytest.raises(ledger.LedgerAuthorityError, match="full owner"):
        ledger.release_finding_lease(
            repo,
            lease_id=lease["lease_id"],
            finding_ids=["f_one"],
            operation_id="release-by-id",
            lease_owner=lease["lease_id"],
            reason="legacy lease id must not authorize release",
        )
    with pytest.raises(ledger.LedgerAuthorityError):
        ledger.release_finding_lease(
            repo,
            lease_id=lease["lease_id"],
            finding_ids=["f_one"],
            operation_id="release-one",
            lease_owner=owner("worker-b"),
            reason="wrong owner",
        )
    with pytest.raises(ledger.LedgerAuthorityError, match="owner_gone"):
        ledger.recover_finding_lease(
            repo,
            lease_id=lease["lease_id"],
            finding_ids=["f_one"],
            operation_id="recover-one",
            recovery_proof={"owner_gone": False, "evidence": []},
        )
    proof = {
        "owner_gone": True,
        "evidence": [
            {"source": "worktree_guard", "status": "gone", "reference": "lease:none"},
            {
                "source": "delivery_liveness",
                "status": "gone",
                "reference": "run:terminal",
            },
            {
                "source": "worker_session",
                "status": "gone",
                "reference": "session:terminal",
            },
        ],
    }
    with pytest.raises(ledger.LedgerAuthorityError, match="owner-terminal"):
        ledger.recover_finding_lease(
            repo,
            lease_id=lease["lease_id"],
            finding_ids=["f_one"],
            operation_id="recover-before-terminal",
            recovery_proof=proof,
        )
    ledger.mark_finding_lease_owner_terminal(
        repo,
        lease_id=lease["lease_id"],
        finding_ids=["f_one"],
        operation_id="lease-owner-terminal",
        terminal_evidence={"terminal": True, "session": "worker-a"},
    )
    recovered = ledger.recover_finding_lease(
        repo,
        lease_id=lease["lease_id"],
        finding_ids=["f_one"],
        operation_id="recover-after-terminal",
        recovery_proof=proof,
    )
    assert recovered["type"] == "finding-lease-recovered"
    assert ledger.active_lease(repo, "f_one") is None


@pytest.mark.skip(reason="(a) audit-campaign drain policy stays in the consumer")
def test_campaign_drain_is_idempotent_scoped_and_same_owner_cleared(
    repo: Path,
) -> None:
    first_owner = owner()
    second_owner = {**owner("worker-b"), "run_id": "run-other"}

    first = ledger.activate_campaign_drain(
        repo,
        campaign_id="3209",
        owner=first_owner,
        operation_id="drain-3209",
        reason="finish the current campaign before producing more findings",
    )
    second = ledger.activate_campaign_drain(
        repo,
        campaign_id="3210",
        owner=second_owner,
        operation_id="drain-3210",
        reason="separate campaign",
    )

    assert (
        ledger.activate_campaign_drain(
            repo,
            campaign_id="3209",
            owner=first_owner,
            operation_id="drain-3209",
            reason="finish the current campaign before producing more findings",
        )
        == first
    )
    with pytest.raises(ledger.LedgerConflict, match="different payload"):
        ledger.activate_campaign_drain(
            repo,
            campaign_id="different",
            owner=first_owner,
            operation_id="drain-3209",
            reason="conflict",
        )
    with pytest.raises(ledger.LedgerConflict, match="active drain"):
        ledger.activate_campaign_drain(
            repo,
            campaign_id="3209",
            owner=first_owner,
            operation_id="another-drain",
            reason="duplicate active binding",
        )
    with pytest.raises(ledger.LedgerAuthorityError, match="same drain owner"):
        ledger.clear_campaign_drain(
            repo,
            campaign_id="3209",
            owner=second_owner,
            operation_id="clear-foreign",
            reason="not mine",
        )

    cleared = ledger.clear_campaign_drain(
        repo,
        campaign_id="3209",
        owner=first_owner,
        operation_id="clear-3209",
        reason="campaign complete",
    )

    assert (
        ledger.clear_campaign_drain(
            repo,
            campaign_id="3209",
            owner=first_owner,
            operation_id="clear-3209",
            reason="campaign complete",
        )
        == cleared
    )
    assert ledger.active_campaign_drain(repo, campaign_id="3209") is None
    assert ledger.active_campaign_drain(repo, campaign_id="3210") == second


@pytest.mark.skip(reason="(a) audit-campaign drain policy stays in the consumer")
def test_old_clear_operation_cannot_clear_a_later_reactivation(repo: Path) -> None:
    first = ledger.activate_campaign_drain(
        repo,
        campaign_id="3209",
        owner=owner(),
        operation_id="activate-first",
        reason="first",
    )
    ledger.clear_campaign_drain(
        repo,
        campaign_id="3209",
        owner=owner(),
        operation_id="clear-shared",
        reason="complete",
    )
    second = ledger.activate_campaign_drain(
        repo,
        campaign_id="3209",
        owner=owner(),
        operation_id="activate-second",
        reason="second",
    )

    with pytest.raises(ledger.LedgerConflict, match="different activation"):
        ledger.clear_campaign_drain(
            repo,
            campaign_id="3209",
            owner=owner(),
            operation_id="clear-shared",
            reason="complete",
        )

    assert first["operation_id"] != second["operation_id"]
    assert ledger.active_campaign_drain(repo, campaign_id="3209") == second


def test_severity_correction_preserves_original_and_closure_authority(
    repo: Path,
) -> None:
    current = finding()
    write_records(repo, current)

    corrected = ledger.append_severity_correction(
        repo,
        finding_id="f_one",
        new_severity="suggestion",
        reason="Impact is cosmetic and has no behavior consequence",
        decision_record="docs/decisions.md#fl-severity-one",
        reviewer_task_id="review-severity",
        owner_approved=False,
        operation_id="severity-one",
        expected_state="open",
        expected_disposition=None,
        expected_digest=ledger.canonical_record_digest(current),
    )

    assert corrected["severity"] == "suggestion"
    assert corrected["original_severity"] == "important"
    assert ledger.closure_authority_severity(corrected) == "important"
    assert ledger.count_live_important(repo) == 0
    for updates in (
        {"original_severity": "suggestion"},
        {"severity_correction": None},
    ):
        with pytest.raises(ledger.LedgerConflict, match="assessment lineage"):
            ledger.append_finding_transition(
                repo,
                "f_one",
                updates,
                operation_id=f"rewrite-{'original' if 'original_severity' in updates else 'correction'}",
                expected_state="open",
                expected_disposition=None,
                expected_digest=ledger.canonical_record_digest(corrected),
            )
    assert (
        ledger.append_severity_correction(
            repo,
            finding_id="f_one",
            new_severity="suggestion",
            reason="Impact is cosmetic and has no behavior consequence",
            decision_record="docs/decisions.md#fl-severity-one",
            reviewer_task_id="review-severity",
            owner_approved=False,
            operation_id="severity-one",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
        )
        == corrected
    )


def test_governed_paths_fail_closed_on_malformed_finding_stream(repo: Path) -> None:
    current = finding()
    write_records(repo, current)
    path = repo / ".audit" / "findings" / "2026-08-07.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    with pytest.raises(ledger.LedgerReadError, match="invalid JSON"):
        ledger.append_finding_transition(
            repo,
            "f_one",
            {"disposition": "candidate-resolved"},
            operation_id="transition-malformed",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
        )
    with pytest.raises(ledger.LedgerReadError, match="invalid JSON"):
        ledger.acquire_finding_lease(
            repo,
            finding_ids=["f_one"],
            coordinator_id="coordinator-3266",
            run_id="run-3284",
            worker_session_id="worker-a",
            worktree="/tmp/worker-a",
            branch="fix/a",
            starting_head="a" * 40,
            work_kind="implementation",
            operation_id="lease-malformed",
        )
    with pytest.raises(ledger.LedgerReadError, match="invalid JSON"):
        ledger.record_terminal_handoff(
            repo,
            operation_id="handoff-malformed",
            mode="evidence-only",
            finding_ids=["f_one"],
            terminal=False,
            terminal_session_id="worker-a",
            terminal_run_id="run-3284",
            branch="fix/a",
            starting_head="a" * 40,
            final_head="a" * 40,
            worktree="/tmp/worker-a",
            worktree_clean=True,
            commits=[],
            pr_number=None,
            merge_commit=None,
            primary_contains_merge=False,
            validations=[],
            accepted_review_evidence=[],
            unresolved_findings=[],
            blockers=[],
        )
    with pytest.raises(ledger.LedgerReadError, match="invalid JSON"):
        ledger.compute_metrics(repo)


def test_critical_or_security_path_severity_correction_is_owner_only(
    repo: Path,
) -> None:
    current = {
        **finding(severity="critical"),
        "anchor": {"path": "fastapi_backend/app/middleware/auth.py"},
    }
    write_records(repo, current)

    with pytest.raises(ledger.LedgerAuthorityError, match="owner"):
        ledger.append_severity_correction(
            repo,
            finding_id="f_one",
            new_severity="important",
            reason="reassessed",
            decision_record="docs/decisions.md#critical",
            reviewer_task_id="review-severity",
            owner_approved=False,
            operation_id="severity-one",
            expected_state="open",
            expected_disposition=None,
            expected_digest=ledger.canonical_record_digest(current),
        )


@pytest.mark.skip(reason="(a) audit-campaign metrics stay in the consumer")
def test_only_explicit_accepted_lineage_changes_underlying_defect_count(
    repo: Path,
) -> None:
    write_records(repo, finding("f_one"), finding("f_two"))
    before = ledger.compute_metrics(repo)

    with pytest.raises(ledger.LedgerAuthorityError, match="accepted"):
        ledger.append_reviewed_lineage(
            repo,
            finding_ids=["f_one", "f_two"],
            review_task_id="review-lineage",
            accepted_review_evidence={"task_id": "review-lineage", "accepted": False},
            operation_id="lineage-rejected",
        )
    ledger.append_reviewed_lineage(
        repo,
        finding_ids=["f_one", "f_two"],
        review_task_id="review-lineage",
        accepted_review_evidence={"task_id": "review-lineage", "accepted": True},
        operation_id="lineage-one",
    )
    after = ledger.compute_metrics(repo)

    assert before["evidence_version_count"] == 2
    assert before["underlying_defect_count"] == 2
    assert after["evidence_version_count"] == 2
    assert after["underlying_defect_count"] == 1


@pytest.mark.skip(reason="(a) audit-campaign metrics stay in the consumer")
def test_metrics_report_current_flow_age_producer_and_zero_width_rates(
    repo: Path,
) -> None:
    first = finding("f_one", ts="2026-08-01T00:00:00+00:00")
    second = finding("f_two", ts="2026-08-02T00:00:00+00:00")
    closed = {
        **first,
        "state": "addressed",
        "ts": "2026-08-03T00:00:00+00:00",
    }
    reopened = {
        **closed,
        "state": "open",
        "ts": "2026-08-04T00:00:00+00:00",
    }
    write_records(repo, first, second, closed, reopened)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 5, tzinfo=UTC)

    metrics = ledger.compute_metrics(repo, start=start, end=end, now=end)
    zero = ledger.compute_metrics(repo, start=end, end=end, now=end)

    assert metrics["live_important_critical"] == 2
    assert metrics["new"] == 2
    assert metrics["closed"] == 1
    assert metrics["reopened"] == 1
    assert metrics["net_change"] == 2
    assert metrics["producer_rate_per_day"] == pytest.approx(0.5)
    assert metrics["consumer_rate_per_day"] == pytest.approx(0.25)
    assert metrics["oldest_unresolved"][0]["finding_id"] == "f_one"
    assert metrics["by_review_task"] == {"review-one": 2}
    assert zero["producer_rate_per_day"] is None
    assert zero["consumer_rate_per_day"] is None


@pytest.mark.skip(reason="(a) audit-campaign metrics stay in the consumer")
def test_candidate_findings_stay_open_and_counted(repo: Path) -> None:
    current = finding(disposition="candidate-resolved")
    write_records(repo, current)

    metrics = ledger.compute_metrics(repo)

    assert metrics["candidate_resolved"] == 1
    assert metrics["live_important_critical"] == 1


# --- P4: provenance-at-write, fenced append, commit-boundary guard ----------


def _transition(repo, current, updates, op="p4-op"):
    return ledger.append_finding_transition(
        repo,
        current["finding_id"],
        updates,
        operation_id=op,
        expected_state=current["state"],
        expected_disposition=current.get("disposition"),
        expected_digest=ledger.canonical_record_digest(current),
    )


def test_addressed_without_provenance_names_missing_fields(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    with pytest.raises(ledger.LedgerConflict, match="missing both"):
        _transition(
            repo,
            current,
            {"state": "addressed", "disposition": "review-confirmed-addressed"},
        )


def test_addressed_with_fix_commit_passes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    updated = _transition(
        repo,
        current,
        {
            "state": "addressed",
            "disposition": "review-confirmed-addressed",
            "fix_commit": "a" * 40,
        },
    )
    assert updated["state"] == "addressed"


def _evidence_finding(*, severity: str = "important") -> dict[str, object]:
    return {
        **finding(severity=severity),
        "snapshot_sha": "d" * 40,
        "snapshot_tree_sha": "e" * 40,
        "anchor": {"path": "src.py", "blob_sha": "f" * 40},
    }


def _deterministic_evidence(
    current: dict[str, object], command: str = "pytest tests/unit/test_boundary.py -q"
) -> dict[str, object]:
    return {
        "schema_version": "deterministic-finding-evidence-v1",
        "evidence_id": "fde_" + "a" * 24,
        "target_snapshot_sha": "b" * 40,
        "target_tree_sha": "c" * 40,
        "commands_sha256": ledger.declared_commands_sha256([command]),
        "results": [
            {
                "declared_command": command,
                "executed_command": command,
                "exit_code": 0,
            }
        ],
        "finding_bindings": [
            {
                "finding_id": current["finding_id"],
                "finding_record_digest": ledger.canonical_record_digest(current),
                "source_snapshot_sha": current["snapshot_sha"],
                "source_tree_sha": current["snapshot_tree_sha"],
                "anchor": current["anchor"],
            }
        ],
        "validated_at": "2026-08-10T08:00:00+00:00",
    }


def test_addressed_with_deterministic_evidence_passes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = _evidence_finding()
    write_records(repo, current)
    evidence = _deterministic_evidence(current)

    updated = _transition(
        repo,
        current,
        {
            "state": "addressed",
            "disposition": "deterministic-evidence-addressed",
            "deterministic_evidence": evidence,
        },
    )

    assert updated["state"] == "addressed"
    assert updated["deterministic_evidence"]["evidence_id"] == evidence["evidence_id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda evidence: evidence.update(evidence_id="not-an-id"),
        lambda evidence: evidence.update(target_tree_sha="short"),
        lambda evidence: evidence["results"][0].update(exit_code=1),
        lambda evidence: evidence.update(commands_sha256="0" * 64),
        lambda evidence: evidence["finding_bindings"][0].update(
            finding_record_digest="0" * 64
        ),
    ],
)
def test_addressed_rejects_malformed_deterministic_evidence(
    tmp_path: Path, mutate
) -> None:
    repo = init_repo(tmp_path / "repo")
    current = _evidence_finding()
    write_records(repo, current)
    evidence = _deterministic_evidence(current, "pytest -q")
    mutate(evidence)

    with pytest.raises(ledger.LedgerConflict, match="deterministic evidence"):
        _transition(
            repo,
            current,
            {
                "state": "addressed",
                "disposition": "deterministic-evidence-addressed",
                "deterministic_evidence": evidence,
            },
        )


def test_addressed_critical_rejects_deterministic_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = _evidence_finding(severity="critical")
    write_records(repo, current)
    with pytest.raises(ledger.LedgerConflict, match="critical"):
        _transition(
            repo,
            current,
            {
                "state": "addressed",
                "disposition": "deterministic-evidence-addressed",
                "deterministic_evidence": _deterministic_evidence(current),
            },
        )


def test_batch_transition_validates_every_finding_before_any_append(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    first = finding("f_first")
    second = finding("f_second")
    write_records(repo, first, second)
    transitions = [
        {
            "finding_id": record["finding_id"],
            "updates": {
                "state": "addressed",
                "disposition": "review-confirmed-addressed",
                "fix_commit": "a" * 40,
            },
            "expected_state": record["state"],
            "expected_disposition": record.get("disposition"),
            "expected_digest": (
                ledger.canonical_record_digest(record) if record is first else "0" * 64
            ),
        }
        for record in (first, second)
    ]

    with pytest.raises(ledger.LedgerConflict, match="expected digest"):
        ledger.append_finding_transitions(
            repo,
            transitions,
            operation_id_prefix="batch-atomic",
        )

    assert [record["state"] for record in ledger.load_finding_records(repo)] == [
        "open",
        "open",
    ]


def _capture_request(
    *findings: dict[str, object],
    producer_id: str = "review-one",
    advisory: bool = False,
) -> dict[str, object]:
    return ledger.build_finding_capture_request(
        producer_kind="governed-review",
        producer_id=producer_id,
        unit_attempt_number=1,
        producer_skill="code-review",
        category="correctness",
        advisory=advisory,
        findings=list(findings),
    )


def _capture_candidate(
    finding_id: str,
    *,
    severity: str = "important",
    advisory: bool = False,
    snapshot_tree_sha: str = "a" * 40,
    review_task_id: str = "review-one",
) -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "review_task_id": review_task_id,
        "producer_skill": "code-review",
        "category": "correctness",
        "snapshot_sha": "b" * 40,
        "snapshot_tree_sha": snapshot_tree_sha,
        "severity": severity,
        "claim": f"claim for {finding_id}",
        "anchor": None,
        "advisory": advisory or None,
        "state": "open",
        "disposition": None,
    }


def test_late_review_capture_is_authoritative_and_has_no_attempt_number() -> None:
    request = ledger.build_finding_capture_request(
        producer_kind="late-review-convergence",
        producer_id="lc_" + "a" * 20,
        unit_attempt_number=None,
        producer_skill="work-issue",
        category="github-review-thread",
        advisory=False,
        findings=[{"severity": "important", "claim": "current thread"}],
    )

    assert request["producer_kind"] == "late-review-convergence"
    assert request["advisory"] is False
    with pytest.raises(ledger.LedgerConflict, match="authoritative"):
        ledger.build_finding_capture_request(
            producer_kind="late-review-convergence",
            producer_id="lc_" + "a" * 20,
            unit_attempt_number=None,
            producer_skill="work-issue",
            category="github-review-thread",
            advisory=True,
            findings=[{"severity": "important", "claim": "current thread"}],
        )


def test_finding_capture_exact_replay_returns_original_receipt(repo: Path) -> None:
    raw = {"severity": "important", "claim": "claim for f_capture"}
    request = _capture_request(raw)
    first = ledger.capture_finding_records(
        repo,
        request=request,
        candidate_records=[_capture_candidate("f_capture")],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )
    replay = ledger.capture_finding_records(
        repo,
        request=request,
        candidate_records=[
            _capture_candidate("f_later_tree", snapshot_tree_sha="c" * 40)
        ],
        source_identity={"snapshot_tree_sha": "c" * 40},
    )

    assert replay == first
    assert first["finding_ids"] == ["f_capture"]
    assert first["type"] == "finding-capture-operation"
    assert first["source_identity"] == {"snapshot_tree_sha": "a" * 40}
    assert first["outcomes"] == [{"finding_id": "f_capture", "status": "created"}]
    history = ledger.load_finding_history(repo, strict_malformed=True)
    assert [record.get("type") for record in history].count(
        "finding-capture-operation"
    ) == 1
    assert [record["finding_id"] for record in ledger.load_finding_records(repo)] == [
        "f_capture"
    ]


def test_finding_capture_conflicting_operation_reuse_writes_nothing(
    repo: Path,
) -> None:
    first_raw = {"severity": "important", "claim": "claim for f_capture"}
    ledger.capture_finding_records(
        repo,
        request=_capture_request(first_raw),
        candidate_records=[_capture_candidate("f_capture")],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )
    before = ledger.load_finding_history(repo, strict_malformed=True)

    with pytest.raises(ledger.LedgerConflict, match="different payload"):
        ledger.capture_finding_records(
            repo,
            request=_capture_request(
                {"severity": "critical", "claim": "changed producer result"}
            ),
            candidate_records=[_capture_candidate("f_changed", severity="critical")],
            source_identity={"snapshot_tree_sha": "c" * 40},
        )

    assert ledger.load_finding_history(repo, strict_malformed=True) == before


def test_finding_capture_rejects_partial_batch_before_any_append(repo: Path) -> None:
    raw = [
        {"severity": "important", "claim": "claim for f_first"},
        {"severity": "invalid", "claim": "claim for f_second"},
    ]
    with pytest.raises(ledger.LedgerConflict, match="unsupported severity"):
        ledger.capture_finding_records(
            repo,
            request=_capture_request(*raw),
            candidate_records=[
                _capture_candidate("f_first"),
                _capture_candidate("f_second", severity="invalid"),
            ],
            source_identity={"snapshot_tree_sha": "a" * 40},
        )

    assert ledger.load_finding_history(repo) == []


def test_finding_capture_normalizes_omitted_optional_fields_for_replay() -> None:
    omitted = _capture_request({"severity": "important", "claim": "same claim"})
    explicit_nulls = _capture_request(
        {
            "severity": "important",
            "claim": "same claim",
            "path": None,
            "line_start": None,
            "line_end": None,
        }
    )

    assert omitted == explicit_nulls


def test_finding_capture_rejects_duplicate_candidate_ids_before_write(
    repo: Path,
) -> None:
    request = _capture_request(
        {"severity": "important", "claim": "claim for f_duplicate"},
        {"severity": "critical", "claim": "claim for f_duplicate"},
    )
    with pytest.raises(ledger.LedgerConflict, match="unique"):
        ledger.capture_finding_records(
            repo,
            request=request,
            candidate_records=[
                _capture_candidate("f_duplicate"),
                _capture_candidate("f_duplicate", severity="critical"),
            ],
            source_identity={"snapshot_tree_sha": "a" * 40},
        )

    assert ledger.load_finding_history(repo) == []


def test_finding_capture_promotes_advisory_in_the_atomic_batch(repo: Path) -> None:
    raw = {"severity": "important", "claim": "claim for f_capture"}
    ledger.capture_finding_records(
        repo,
        request=_capture_request(raw, producer_id="local-one", advisory=True),
        candidate_records=[
            _capture_candidate("f_capture", advisory=True, review_task_id="local-one")
        ],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )
    receipt = ledger.capture_finding_records(
        repo,
        request=_capture_request(raw, producer_id="formal-one"),
        candidate_records=[
            _capture_candidate("f_capture", review_task_id="formal-one")
        ],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )

    [promoted] = ledger.load_finding_records(repo, strict_malformed=True)
    assert promoted["advisory"] is None
    assert promoted["capture_record_kind"] == "promotion"
    assert promoted["capture_operation_id"] == receipt["operation_id"]
    assert receipt["outcomes"] == [{"finding_id": "f_capture", "status": "promoted"}]


def test_finding_capture_reopens_a_failed_review_finding_for_new_producer(
    repo: Path,
) -> None:
    raw = {"severity": "important", "claim": "claim for f_capture"}
    first = ledger.capture_finding_records(
        repo,
        request=_capture_request(raw, producer_id="failed-review"),
        candidate_records=[
            _capture_candidate("f_capture", review_task_id="failed-review")
        ],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )
    ledger.retire_failed_review_findings(
        repo,
        producer_id="failed-review",
        expected_finding_ids=list(first["finding_ids"]),
        supersession_digest="b" * 64,
        verdict_digest="c" * 64,
    )

    receipt = ledger.capture_finding_records(
        repo,
        request=_capture_request(raw, producer_id="replacement-review"),
        candidate_records=[
            _capture_candidate("f_capture", review_task_id="replacement-review")
        ],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )

    [reopened] = ledger.load_finding_records(repo, strict_malformed=True)
    assert reopened["state"] == "open"
    assert reopened["disposition"] is None
    assert reopened["review_task_id"] == "replacement-review"
    assert receipt["outcomes"] == [{"finding_id": "f_capture", "status": "reopened"}]


def test_finding_capture_failed_replace_preserves_previous_segment(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_records(repo, finding("f_existing"))
    ledger_path = repo / ".audit" / "findings" / "2026-08-07.jsonl"
    before = ledger_path.read_bytes()
    monkeypatch.setattr(
        ledger.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(ledger.LedgerReadError, match="replace failed"):
        ledger.capture_finding_records(
            repo,
            request=_capture_request(
                {"severity": "important", "claim": "claim for f_capture"}
            ),
            candidate_records=[_capture_candidate("f_capture")],
            source_identity={"snapshot_tree_sha": "a" * 40},
        )

    assert ledger_path.read_bytes() == before
    assert [record["finding_id"] for record in ledger.load_finding_records(repo)] == [
        "f_existing"
    ]


def test_finding_capture_rejects_unterminated_existing_segment(repo: Path) -> None:
    directory = repo / ".audit" / "findings"
    directory.mkdir(parents=True)
    ledger_path = directory / f"{ledger._now():%Y-%m-%d}.jsonl"
    original = json.dumps(finding("f_existing"), sort_keys=True).encode("utf-8")
    ledger_path.write_bytes(original)

    with pytest.raises(ledger.LedgerReadError, match="unterminated"):
        ledger.capture_finding_records(
            repo,
            request=_capture_request(
                {"severity": "important", "claim": "claim for f_capture"}
            ),
            candidate_records=[_capture_candidate("f_capture")],
            source_identity={"snapshot_tree_sha": "a" * 40},
        )

    assert ledger_path.read_bytes() == original


def test_finding_capture_concurrent_replay_publishes_one_batch(repo: Path) -> None:
    raw = {"severity": "important", "claim": "claim for f_capture"}
    request = _capture_request(raw)

    def capture() -> dict[str, object]:
        return ledger.capture_finding_records(
            repo,
            request=request,
            candidate_records=[_capture_candidate("f_capture")],
            source_identity={"snapshot_tree_sha": "a" * 40},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _index: capture(), range(2)))

    assert receipts[0] == receipts[1]
    history = ledger.load_finding_history(repo, strict_malformed=True)
    assert len(history) == 2
    assert {record.get("type") for record in history} == {
        None,
        "finding-capture-operation",
    }


def test_strict_reader_rejects_capture_rows_without_terminal_receipt(
    repo: Path,
) -> None:
    write_records(
        repo,
        {
            **finding("f_partial"),
            "capture_operation_id": "finding-capture-v1-partial",
            "capture_record_kind": "finding",
        },
    )

    with pytest.raises(ledger.LedgerReadError, match="terminal receipt"):
        ledger.load_finding_records(repo, strict_malformed=True)


def test_later_transition_does_not_inherit_capture_membership(repo: Path) -> None:
    raw = {"severity": "important", "claim": "claim for f_capture"}
    ledger.capture_finding_records(
        repo,
        request=_capture_request(raw),
        candidate_records=[_capture_candidate("f_capture")],
        source_identity={"snapshot_tree_sha": "a" * 40},
    )
    [captured] = ledger.load_finding_records(repo, strict_malformed=True)

    transitioned = ledger.append_finding_transition(
        repo,
        "f_capture",
        {"category": "reviewed-correctness"},
        operation_id="transition-after-capture",
        expected_state="open",
        expected_disposition=None,
        expected_digest=ledger.canonical_record_digest(captured),
    )

    assert "capture_operation_id" not in transitioned
    assert "capture_record_kind" not in transitioned
    [latest] = ledger.load_finding_records(repo, strict_malformed=True)
    assert latest == transitioned


def test_addressed_with_malformed_fix_commit_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    with pytest.raises(ledger.LedgerConflict, match="40-hex"):
        _transition(
            repo,
            current,
            {
                "state": "addressed",
                "disposition": "review-confirmed-addressed",
                "fix_commit": "abc123",
            },
        )


def test_waived_requires_decision_record_and_reason(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    with pytest.raises(ledger.LedgerConflict, match="waiver_decision_record"):
        _transition(repo, current, {"state": "waived", "disposition": "waived-dr"})
    updated = _transition(
        repo,
        current,
        {
            "state": "waived",
            "disposition": "waived-dr",
            "waiver_decision_record": "FL-R-999",
            "waiver_reason": "duplicate of canonical record",
        },
        op="p4-op-2",
    )
    assert updated["state"] == "waived"


def test_terminal_state_requires_disposition(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    with pytest.raises(ledger.LedgerConflict, match="non-empty `disposition`"):
        _transition(repo, current, {"state": "stale"})


def test_failed_review_retirement_stales_the_exact_open_producer_batch(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    review_open = finding("f_review_open", severity="critical")
    review_suggestion = finding("f_review_suggestion", severity="suggestion")
    review_closed = finding(
        "f_review_closed",
        state="addressed",
        disposition="review-confirmed-addressed",
    ) | {"fix_commit": "a" * 40}
    other_review = finding("f_other_review") | {"review_task_id": "review-two"}
    replayed_other_producer = finding("f_shared") | {"review_task_id": "review-two"}
    write_records(
        repo,
        review_open,
        review_suggestion,
        review_closed,
        other_review,
        replayed_other_producer,
    )

    receipt = ledger.retire_failed_review_findings(
        repo,
        producer_id="review-one",
        expected_finding_ids=[
            "f_review_open",
            "f_review_suggestion",
            "f_review_closed",
            "f_shared",
        ],
        supersession_digest="b" * 64,
        verdict_digest="c" * 64,
    )

    latest = {
        record["finding_id"]: record for record in ledger.load_finding_records(repo)
    }
    assert latest["f_review_open"]["state"] == "stale"
    assert latest["f_review_suggestion"]["state"] == "stale"
    assert latest["f_review_open"]["disposition"] == "failed-review-superseded"
    assert latest["f_review_closed"] == review_closed
    assert latest["f_other_review"] == other_review
    assert latest["f_shared"] == replayed_other_producer
    assert receipt["finding_ids"] == [
        "f_review_closed",
        "f_review_open",
        "f_review_suggestion",
        "f_shared",
    ]
    assert receipt["write_count"] == 2


def test_failed_review_retirement_is_atomic_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    first = finding("f_first")
    second = finding("f_second")
    write_records(repo, first, second)
    original_replace = ledger.os.replace

    def crash_before_publish(source: Path, target: Path) -> None:
        del source, target
        raise OSError("simulated crash")

    monkeypatch.setattr(ledger.os, "replace", crash_before_publish)
    with pytest.raises(ledger.LedgerReadError, match="simulated crash"):
        ledger.retire_failed_review_findings(
            repo,
            producer_id="review-one",
            expected_finding_ids=["f_first", "f_second"],
            supersession_digest="d" * 64,
            verdict_digest="e" * 64,
        )
    assert ledger.load_finding_records(repo) == [first, second]

    monkeypatch.setattr(ledger.os, "replace", original_replace)
    receipt = ledger.retire_failed_review_findings(
        repo,
        producer_id="review-one",
        expected_finding_ids=["f_first", "f_second"],
        supersession_digest="d" * 64,
        verdict_digest="e" * 64,
    )
    history_size = len(ledger.load_finding_history(repo))
    assert (
        ledger.retire_failed_review_findings(
            repo,
            producer_id="review-one",
            expected_finding_ids=["f_first", "f_second"],
            supersession_digest="d" * 64,
            verdict_digest="e" * 64,
        )
        == receipt
    )
    assert len(ledger.load_finding_history(repo)) == history_size


def test_lineage_reference_must_resolve(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    with pytest.raises(ledger.LedgerConflict, match="does not reference"):
        _transition(
            repo,
            current,
            {
                "state": "waived",
                "disposition": "waived-dr",
                "waiver_decision_record": "FL-R-1",
                "waiver_reason": "dup",
                "duplicate_of": "f_missing",
            },
        )


def test_two_writers_one_cas_second_rejected_at_fenced_append(
    tmp_path: Path,
) -> None:
    """Race reproduction: both writers read the same digest; the fenced
    validate+append lets exactly one through — the loser's CAS fails."""
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    provenance = {
        "state": "addressed",
        "disposition": "review-confirmed-addressed",
        "fix_commit": "b" * 40,
    }
    _transition(repo, current, provenance, op="writer-one")
    with pytest.raises(ledger.LedgerConflict, match="expected digest|expected state"):
        _transition(repo, current, provenance, op="writer-two")


def test_torn_append_fails_closed_and_scan_reports_it(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    current = finding()
    write_records(repo, current)
    shard = repo / ".audit" / "findings" / "2026-08-07.jsonl"
    with shard.open("a", encoding="utf-8") as fh:
        fh.write('{"finding_id": "f_torn", "state": "op')  # crash mid-append
    # Strict readers (the write path) fail closed — no silent corruption.
    with pytest.raises(ledger.LedgerReadError, match="invalid JSON"):
        ledger.load_finding_history(repo, strict_malformed=True)
    report = ledger.scan_terminal_provenance(repo)
    assert report["torn_tail_shards"], "scan must surface the torn tail"


def test_scan_clean_ledger_reports_zero(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    write_records(
        repo,
        finding(),
        finding(
            "f_two",
            state="addressed",
            disposition="review-confirmed-addressed",
        )
        | {"fix_commit": "c" * 40},
    )
    report = ledger.scan_terminal_provenance(repo)
    assert report["records"] == 2
    assert report["violations"] == []


# --- P2: suggestion-TTL bulk expiry -----------------------------------------


@pytest.mark.skip(reason="(a) stale campaign-suggestion expiry stays in the consumer")
def test_expire_suggestions_bulk_single_operation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    stale_a = finding("f_sug_a", severity="suggestion", ts="2026-07-01T00:00:00+00:00")
    stale_b = finding("f_sug_b", severity="suggestion", ts="2026-07-02T00:00:00+00:00")
    fresh = finding("f_sug_fresh", severity="suggestion", ts=ledger._timestamp())
    important = finding("f_imp", ts="2026-07-01T00:00:00+00:00")
    write_records(repo, stale_a, stale_b, fresh, important)

    dry = ledger.expire_stale_suggestions(repo, operation_id="sweep-dry", dry_run=True)
    assert dry == ["f_sug_a", "f_sug_b"]
    assert len(ledger.load_finding_history(repo)) == 4  # dry-run wrote nothing

    expired = ledger.expire_stale_suggestions(repo, operation_id="sweep-one")
    assert expired == ["f_sug_a", "f_sug_b"]
    latest = {r["finding_id"]: r for r in ledger.load_finding_records(repo)}
    assert latest["f_sug_a"]["state"] == "stale"
    assert latest["f_sug_a"]["disposition"] == "suggestion-ttl-expired"
    assert latest["f_sug_a"]["operation_id"] == "sweep-one"
    assert latest["f_sug_b"]["operation_id"] == "sweep-one"  # ONE bulk operation
    assert latest["f_sug_fresh"]["state"] == "open"
    assert latest["f_imp"]["state"] == "open"  # important+ never TTL-expired

    # Idempotent replay: same operation returns the same result, appends nothing.
    replay = ledger.expire_stale_suggestions(repo, operation_id="sweep-one")
    assert replay == ["f_sug_a", "f_sug_b"]
    assert len(ledger.load_finding_history(repo)) == 6


@pytest.mark.skip(reason="(a) agent-dispatch campaign admission is deleted at cutover B")
def test_audit_launch_blocked_at_ceiling_names_count(tmp_path: Path) -> None:
    import importlib.util as _ilu
    import sys as _sys

    scripts_dir = Path(__file__).resolve().parents[4] / "scripts" / "util"
    _sys.path.insert(0, str(scripts_dir))
    spec = _ilu.spec_from_file_location(
        "agent_dispatch_p2", scripts_dir / "agent_dispatch.py"
    )
    dispatch = _ilu.module_from_spec(spec)
    _sys.modules[spec.name] = dispatch
    spec.loader.exec_module(dispatch)

    under_repo = init_repo(tmp_path / "under-repo")
    write_records(
        under_repo,
        *[finding(f"f_{i}", severity="important") for i in range(129)],
    )
    dispatch.validate_discovery_admission(
        {"work_kind": "implementation", "category": "backend-audit"},
        worktree=under_repo,
    )

    repo = init_repo(tmp_path / "repo")
    write_records(
        repo,
        *[finding(f"f_{i}", severity="important") for i in range(130)],
    )
    with pytest.raises(dispatch.DispatchError, match="130 live important"):
        dispatch.validate_discovery_admission(
            {"work_kind": "implementation", "category": "backend-audit"},
            worktree=repo,
        )
    # Immutable review_intent prevents a discovery launch from bypassing the
    # brake by choosing a category without audit/discovery keywords.
    with pytest.raises(dispatch.DispatchError, match="130 live important"):
        dispatch.validate_discovery_admission(
            {
                "work_kind": "review",
                "review_intent": "discovery",
                "category": "backend",
            },
            worktree=repo,
        )
    # Adjudication drains inventory: never blocked by the brake.
    dispatch.validate_discovery_admission(
        {
            "work_kind": "review",
            "review_intent": "resolution-adjudication",
            "category": "audit",
        },
        worktree=repo,
    )
    # Non-audit implementation work is untouched.
    dispatch.validate_discovery_admission(
        {"work_kind": "implementation", "category": "backend"},
        worktree=repo,
    )
    # Audit-shaped dispatch must fail closed when the authoritative ledger is
    # absent, matching the issue-availability guard.
    missing_repo = init_repo(tmp_path / "missing-repo")
    with pytest.raises(
        dispatch.DispatchError, match="audit launch blocked:.*primary Finding Ledger"
    ):
        dispatch.validate_discovery_admission(
            {
                "work_kind": "review",
                "review_intent": "discovery",
                "category": "backend",
            },
            worktree=missing_repo,
        )


@pytest.mark.parametrize(
    ("contract", "outcome", "expired"),
    [
        (None, "merged", False),
        ("loop-zero-v1", "in_progress", False),
        ("loop-zero-v1", "merged", True),
        ("loop-zero-v1", "abandoned", False),
    ],
)
def test_only_verified_merged_new_run_findings_expire_from_debt(
    repo, contract, outcome, expired
):
    run_id = "sr_" + "1" * 32
    record = {**finding(), "delivery_run_id": run_id}
    write_records(repo, record, finding("legacy"))
    runs = repo / ".audit/skill-runs"
    runs.mkdir()
    start = {"run_id": run_id, "outcome": "in_progress"}
    if contract:
        start["delivery_contract"] = contract
    (runs / "2026-09-10.jsonl").write_text(
        json.dumps(start) + "\n" + json.dumps({**start, "outcome": outcome}) + "\n"
    )
    original = (repo / ".audit/findings/2026-08-07.jsonl").read_bytes()
    assert ledger.count_live_important(repo) == (1 if expired else 2)
    assert ledger.load_finding_history(repo) == [record, finding("legacy")]
    assert (repo / ".audit/findings/2026-08-07.jsonl").read_bytes() == original


def test_unknown_or_conflicting_run_keeps_findings_visible(repo):
    run_id = "sr_" + "2" * 32
    write_records(repo, {**finding(), "delivery_run_id": run_id})
    assert ledger.count_live_important(repo) == 1
    runs = repo / ".audit/skill-runs"
    runs.mkdir()
    (runs / "2026-09-10.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "run_id": run_id,
                    "delivery_contract": "intelflo-v1",
                    "outcome": "in_progress",
                },
                {
                    "run_id": run_id,
                    "delivery_contract": "loop-zero-v1",
                    "outcome": "merged",
                },
            ]
        )
    )
    assert ledger.count_live_important(repo) == 1
