from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority_store
from loopzero.kernel.review_state import ReviewSlotReservation, ReviewSlotSettlement
from loopzero.review.admission import Reserved, _launch_reasons
from loopzero.review.stats import review_stats
from loopzero.review.telemetry import (
    ReviewLaunchOutcomeV1,
    ReviewLaunchV1,
    ReviewTelemetryError,
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
    ("family", "slot", "transition", "security", "retry", "expected"),
    [
        ("delivery", "primary", "initial", False, False, "initial"),
        ("delivery", "delta", "substantive", False, False, "bounded-delta"),
        ("delivery", "primary", "supersession", False, False, "supersession"),
        ("trust", "primary", "initial", False, False, "trust-verification"),
        ("trust", "delta", "substantive", False, False, "trust-delta"),
        ("delivery", "primary", "initial", True, False, "security-path"),
        ("delivery", "primary", "owner-requested", False, False, "owner-requested"),
        ("delivery", "primary", "initial", False, True, "infrastructure-retry"),
    ],
)
def test_every_launch_reason_is_derived(
    family, slot, transition, security, retry, expected
):
    primary, secondary = _launch_reasons(
        family=family,
        slot_kind=slot,
        transition_kind=transition,
        security_triggered=security,
        infrastructure_retry=retry,
    )
    assert primary == expected
    assert primary not in secondary


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


def test_review_observations_survive_with_reservations_during_compaction():
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
    rows = [reservation, launch, outcome]
    retained = authority_store._retention_live_record_ids(rows)
    assert {id(row) for row in rows} <= retained
