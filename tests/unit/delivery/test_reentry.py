import pytest

from loopzero.delivery import reentry


def test_reentry_generation_and_pending_projection_are_preserved(monkeypatch):
    records = [
        {"type": "delivery-control", "action": "review-reentry", "run_id": "run"},
        {"type": "delivery-control", "action": "review-reentry", "run_id": "run"},
    ]
    monkeypatch.setattr(
        "loopzero.review.authority.delivery_controller_records", lambda rows: rows
    )
    assert reentry.publication_generation(records, "run") == 2
    assert reentry.pending_reentry(records, [], "run") is records[0]


def test_stale_projection_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr("loopzero.kernel.run_log.load_phase_events", lambda root: [])
    monkeypatch.setattr("loopzero.kernel.run_log.phase_state", lambda *args, **kwargs: ("done", None))
    with pytest.raises(reentry.StaleReentryProjection):
        reentry.append_reentry_phase(
            root=tmp_path, skill="review", run_id="run", record={"reason": "repair"}
        )
