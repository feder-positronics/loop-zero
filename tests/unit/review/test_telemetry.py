from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority_store
from loopzero.kernel.review_state import ReviewSlotReservation, ReviewSlotSettlement
from loopzero.review.admission import Reserved
from loopzero.review.stats import review_stats
from loopzero.review.telemetry import (
    ReviewLaunchOutcomeV1,
    ReviewLaunchV1,
    ReviewTelemetryError,
    billing_mode_for_credential_kind,
)
from loopzero.review.trust_claims import Invalidation
from loopzero.runners.contract import (
    ReviewOutcome,
    RuntimeCostSource,
    RuntimeResult,
    RuntimeStatus,
    RuntimeUsage,
    TerminalReason,
)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("oauth-file", "subscription"),
        ("oauth", "subscription"),
        ("subscription", "subscription"),
        ("claude-oauth", "subscription"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "subscription"),
        ("sk-ant-oat01-" + "x" * 80, "subscription"),
        ("setup-token-file", "subscription"),
        ("token-env", "subscription"),
        ("token-file", "subscription"),
        ("token-file(default)", "subscription"),
        ("codex-auth-file", "subscription"),
        ("codex-oauth", "subscription"),
        ("chatgpt", "subscription"),
        ("chatgpt-login", "subscription"),
        ("cursor-auth-file", "subscription"),
        ("cursor-login", "subscription"),
        ("cursor-browser-login", "subscription"),
        ("ANTHROPIC_API_KEY", "metered"),
        ("anthropic-api-key", "metered"),
        ("sk-ant-api03-" + "x" * 80, "metered"),
        ("OPENAI_API_KEY", "metered"),
        ("openai-api-key", "metered"),
        ("api-key", "metered"),
        ("metered", "metered"),
        ("console", "unknown"),
        ("bearer-token", "unknown"),
        (None, "unknown"),
    ],
)
def test_billing_mode_is_derived_only_from_credential_evidence(kind, expected):
    assert billing_mode_for_credential_kind(kind) == expected


def _reserved(*, caller_label=False):
    patch = {
        "schema_version": "patch-identity-v1",
        "base_sha": "0" * 40,
        "base_tree_sha": "1" * 40,
        "candidate_sha": "2" * 40,
        "candidate_tree_sha": "3" * 40,
        "diff_format": "git-binary-full-index-no-renames-v1",
        "diff_sha256": "4" * 64,
        "patch_id_verbatim": "5" * 40,
    }
    task = {
        "review_intent": "trust-manifest-verification",
        "source_identity": {"head": "2" * 40},
        "patch_identity": patch,
        "manifest_sha256": "6" * 64,
        "engine": "sol",
        "pr_number": 39,
    }
    if caller_label:
        task["launch_reason"] = "initial"
    reservation = ReviewSlotReservation(
        generation_id="cg_" + "1" * 32,
        family="trust",
        slot_kind="primary",
        task_id="task",
        idempotency_key="key",
        reservation_id="rr_" + "2" * 32,
    )
    return Reserved(
        generation=SimpleNamespace(
            generation_id=reservation.generation_id,
            changed_paths=("src/a.py",),
            dependency_paths=("src/b.py",),
        ),
        slot=reservation,
        scoped_task=task,
        records_to_append=(reservation.to_dict(),),
        launch_reason="trust-verification",
        secondary_triggers=("initial",),
    )


def test_launch_rejects_caller_reason_and_hashes_paths():
    with pytest.raises(TypeError, match="reason"):
        ReviewLaunchV1(reason="initial")
    with pytest.raises(ReviewTelemetryError, match="package-derived"):
        ReviewLaunchV1.from_admission(_reserved(caller_label=True), attempt_id="a1")

    invalidation = Invalidation(
        fresh_claim_ids=("fresh",),
        carried_claims={"carried": object()},
        retired_claim_ids=(),
        causes={"fresh": "text-or-coverage-changed"},
        changed_paths=("src/a.py",),
    )
    launch = ReviewLaunchV1.from_admission(
        _reserved(),
        attempt_id="a1",
        admitted_at=datetime(2026, 9, 14, tzinfo=UTC),
        invalidation=invalidation,
    )
    row = launch.to_dict()
    assert ReviewLaunchV1.from_mapping(row).to_dict() == row
    assert row["fresh_claim_count"] == 1
    assert row["carried_claim_count"] == 1
    assert row["changed_path_count"] == 1
    assert row["covered_path_count"] == 2
    assert "changed_paths" not in row
    assert "covered_paths" not in row


def test_unknown_cost_and_credential_kind_never_become_zero():
    launch = ReviewLaunchV1.from_admission(
        _reserved(), attempt_id="a1", admitted_at="2026-09-14T10:00:00Z"
    )
    settlement = ReviewSlotSettlement(
        reservation_id=launch.reservation_id,
        generation_id="cg_" + "1" * 32,
        family="trust",
        slot_kind="primary",
        task_id="task",
        outcome=ReviewOutcome.RELEASED,
        terminal_ref="7" * 64,
        settlement_id="rs_" + "8" * 32,
    )
    result = RuntimeResult(
        vendor="codex",
        transport="cli",
        requested_model="gpt",
        status=RuntimeStatus.FAILED,
        terminal_reason=TerminalReason.TRANSPORT_DISCONNECT,
        attempt_id="a1",
        usage=RuntimeUsage(input_tokens=10),
        cost_usd=None,
        cost_source=RuntimeCostSource.UNKNOWN,
        duration_s=2.5,
    )
    outcome = ReviewLaunchOutcomeV1.from_settlement(
        launch, settlement, result, terminal_at="2026-09-14T10:00:03Z"
    ).to_dict()
    assert ReviewLaunchOutcomeV1.from_mapping(outcome).to_dict() == outcome
    assert outcome["api_equivalent_usd"] is None
    assert outcome["cost_source"] == "unknown"
    assert outcome["billing_mode"] == "unknown"
    totals = review_stats([launch.to_dict(), outcome]).totals
    assert totals.api_equivalent_usd is None
    assert totals.known_api_equivalent_usd == 0
    assert totals.cost_unknown == 1

    malformed = {**outcome, "api_equivalent_usd": 0.0}
    with pytest.raises(ReviewTelemetryError, match="known source"):
        ReviewLaunchOutcomeV1.from_mapping(malformed)
    malformed_totals = review_stats([launch.to_dict(), malformed]).totals
    assert malformed_totals.api_equivalent_usd is None
    assert malformed_totals.known_api_equivalent_usd == 0
    assert malformed_totals.cost_unknown == 1


def test_review_observations_survive_with_reservations_during_compaction(
    monkeypatch,
):
    launch = ReviewLaunchV1.from_admission(
        _reserved(), attempt_id="a1", admitted_at="2026-09-14T10:00:00Z"
    ).to_dict()
    outcome = {
        **launch,
        "type": "review-launch-outcome-v1",
        "review_outcome": "released",
        "terminal_at": "2026-09-14T10:00:01Z",
    }
    reservation = _reserved().slot.to_dict()
    launch["schema_version"] = "telemetry-before-review-stats"
    outcome["schema_version"] = "telemetry-before-review-stats"
    rows = [reservation, launch, outcome]
    monkeypatch.setattr(
        authority_store,
        "_authenticated_coordinator_record_ids",
        lambda records: frozenset(map(id, records)),
    )
    monkeypatch.setattr(
        authority_store,
        "delivery_controller_records",
        lambda records: [],
    )
    from loopzero.kernel import authority_projection

    monkeypatch.setattr(
        authority_projection,
        "_authenticated_coordinator_record_ids",
        lambda records: frozenset(map(id, records)),
    )
    retained = authority_store.retained_authority_projection(rows)
    assert reservation in retained
    assert launch in retained
    assert outcome in retained
