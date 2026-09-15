import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loopzero.kernel import authority_projection, authority_store
from loopzero.kernel.policy import (
    DISPATCH_POLICY_VERSION,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)
from loopzero.review import admission, authority as review_authority
from loopzero.review.stats import review_stats


@pytest.fixture
def measurement_ledger():
    pr_counts = {
        4396: 38,
        4381: 14,
        4379: 13,
        4375: 8,
        4402: 5,
        4385: 5,
        4378: 4,
        4390: 3,
        4403: 2,
        4404: 2,
        4405: 2,
        4406: 1,
        4407: 1,
        4408: 1,
        4409: 1,
        4410: 1,
        4411: 1,
        4412: 1,
        4413: 0,
        4422: 0,
    }
    assigned_prs = [pr for pr, count in pr_counts.items() for _ in range(count)]
    rows = []
    base_time = datetime(2026, 9, 14, 8, tzinfo=UTC)
    for offset, pr in enumerate(pr_counts):
        rows.append(
            {
                "type": "pull-request-merged",
                "pr_number": pr,
                "merged_at": (base_time + timedelta(minutes=offset)).isoformat(),
            }
        )
    failure_classes = [
        "engine-output",
        "engine-output",
        "engine-output",
        "engine-output",
        "acceptance-environment",
        "model-result",
    ]
    engines = ("claude", "codex", "cursor")
    for index, pr in enumerate(assigned_prs):
        if 24 <= index < 75:
            intent = "delivery-code-review"
            local = index - 24
            source = f"delivery-source-{local}"
            patch = "delivery-patch-0" if local == 1 else f"delivery-patch-{local}"
            manifest = None
            duration = 16_380 / 51
            cost = 14.1 / 51
            failure = failure_classes[local] if local < 6 else None
        elif index < 24 or index < 100:
            intent = "trust-manifest-verification"
            local = index if index < 24 else index - 51
            source = "trust-source-0" if local <= 12 else f"trust-source-{local}"
            patch = None
            manifest = f"manifest-{local}"
            duration = 17_100 / 49
            cost = 19.6 / 49
            failure = None
        else:
            intent = "review-adjudication"
            local = index - 100
            source = f"other-source-{local}"
            patch = None
            manifest = None
            duration = 720 / 3
            cost = 0.6 / 3
            failure = "operator-terminated" if local == 0 else None
        task_id = f"review-{index}"
        common = {
            "task_id": task_id,
            "attempt_index": 0,
            "run_id": f"run-{index}",
            "pr_number": pr,
            "review_intent": intent,
            "engine": engines[index % len(engines)],
            "ts": (base_time + timedelta(seconds=index)).isoformat(),
        }
        start = {**common, "type": "attempt-start", "source_identity": source}
        if patch is not None:
            start["patch_identity_digest"] = patch
        if manifest is not None:
            start["manifest_sha256"] = manifest
        rows.append(start)
        terminal = {
            **common,
            "type": "attempt-terminal",
            "status": "failed" if failure else "completed",
            "duration_s": duration,
            "cost_usd": cost,
            "input_tokens": 100,
            "output_tokens": 10,
        }
        if failure:
            terminal["failure_class"] = failure
        rows.append(terminal)
        if not failure:
            rows.append({**common, "type": "verdict", "verdict": "pass"})
    return rows


def test_2026_09_14_measurement_golden_shape(measurement_ledger):
    actual = review_stats(measurement_ledger, last_merged=20).to_dict()
    measurement_fields = (
        "terminals",
        "failures",
        "unchanged_content_repetitions",
        "minutes",
        "api_equivalent_usd",
    )
    selected = {
        "totals": {
            key: actual["totals"][key]
            for key in (
                "starts",
                "terminals",
                "verdicts",
                "failures",
                "distinct_source_identities",
                "distinct_patch_identities",
                "distinct_manifest_identities",
                "unchanged_content_repetitions",
                "minutes",
                "api_equivalent_usd",
            )
        },
        "per_intent": {
            intent: {key: row[key] for key in measurement_fields}
            for intent, row in actual["per_intent"].items()
        },
        "reason_keys": sorted(actual["per_reason"]),
        "failures_by_class": actual["failures_by_class"],
        "pr_starts": {pr: row["starts"] for pr, row in actual["per_pr"].items()},
        "percentiles": actual["percentiles"],
    }
    golden = json.loads(
        (
            Path(__file__).parents[2] / "fixtures" / "review-stats-2026-09-14.json"
        ).read_text()
    )
    assert selected == golden


def test_pre_release_ledger_admits_and_projects_as_unrecorded(tmp_path, monkeypatch):
    metadata = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "policy_version": DISPATCH_POLICY_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
    }
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
    rows = [
        {
            **metadata,
            "type": "attempt-start",
            "task_id": "old-review",
            "attempt_index": 0,
            "run_id": "old-run",
            "work_unit_id": "code-review",
        },
        {
            **metadata,
            "type": "attempt-terminal",
            "task_id": "old-review",
            "attempt_index": 0,
            "run_id": "old-run",
            "work_unit_id": "code-review",
            "review_lens": "code",
            "repository_binding": "repo",
            "snapshot_tree_sha": patch["candidate_tree_sha"],
            "patch_identity": patch,
            "status": "completed",
            "reason": "legacy-terminal-label",
        },
        {
            **metadata,
            "type": "verdict",
            "task_id": "old-review",
            "run_id": "old-run",
            "work_unit_id": "code-review",
            "verdict": "pass",
        },
    ]
    terminal = rows[1]
    verdict = rows[2]
    monkeypatch.setattr(
        authority_projection,
        "seam_accepted_review_terminals",
        lambda records: {"old-review": terminal},
    )
    monkeypatch.setattr(
        authority_projection,
        "seam_authenticated_verdicts",
        lambda records, **kwargs: {"old-review": verdict},
    )
    monkeypatch.setattr(
        review_authority, "authenticated_review_verdict", lambda *args: "pass"
    )
    monkeypatch.setattr(
        admission,
        "tree_diff_paths",
        lambda repository, source, target: ((), patch["diff_sha256"]),
    )
    monkeypatch.setattr(
        admission,
        "security_trigger_paths_between",
        lambda repository, source, target: (),
    )
    monkeypatch.setattr(authority_store, "delivery_controller_records", lambda rows: [])

    with authority_store.authority_ledger_lock(tmp_path):
        admitted = admission.admit_review(
            tmp_path,
            rows,
            repository_binding="repo",
            task={
                "task_id": "adopted-review",
                "idempotency_key": "adopted-review",
                "review_intent": "delivery-code-review",
            },
            current_source_identity={"head": patch["candidate_sha"]},
            current_tree_sha=patch["candidate_tree_sha"],
            patch_identity=patch,
            required_sections=("code",),
            equivalence_proof=None,
            format_only_proof=None,
            requested="review",
            changed_paths=None,
            security_trigger_paths=(),
        )
    assert isinstance(admitted, admission.Carry)

    retained = authority_store.retained_authority_projection(
        rows, active_run_ids={"old-run"}
    )
    result = review_stats(retained).to_dict()
    assert result["per_pr"].keys() == {"unrecorded"}
    assert result["per_intent"].keys() == {"unrecorded"}
    assert result["per_reason"].keys() == {"unrecorded"}
    assert result["per_engine"].keys() == {"unrecorded"}
    assert result["totals"]["source_identity_unrecorded"] == 1
    assert result["totals"]["patch_identity_unrecorded"] == 1
    assert result["totals"]["manifest_identity_unrecorded"] == 1
    assert result["totals"]["claim_counts_unrecorded"] == 1
    assert result["totals"]["api_equivalent_usd"] is None
    assert result["totals"]["cost_unknown"] == 1


def test_since_keeps_attempts_with_unrecorded_timestamps():
    rows = [
        {
            "type": "attempt-start",
            "task_id": "undated-review",
            "attempt_index": 0,
            "run_id": "undated-run",
            "work_unit_id": "code-review",
        }
    ]
    result = review_stats(rows, since="2026-09-14T00:00:00Z")
    assert result.totals.starts == 1
    assert result.per_pr["unrecorded"].starts == 1


def test_launch_start_terminal_and_outcome_join_as_one_attempt():
    common = {
        "task_id": "joined-review",
        "attempt_index": 2,
        "attempt_id": "joined-review:2",
        "review_intent": "delivery-code-review",
        "pr_number": 4409,
        "engine": "codex",
    }
    rows = [
        {
            **common,
            "type": "review-launch-v1",
            "reservation_id": "rr_joined",
            "reason": "initial",
            "source_identity_digest": "1" * 64,
            "patch_identity_digest": "2" * 64,
            "manifest_digest": None,
            "fresh_claim_count": 0,
            "carried_claim_count": 0,
            "admitted_at": "2026-09-14T10:00:00+00:00",
        },
        {**common, "type": "attempt-start", "run_id": "run"},
        {
            **common,
            "type": "attempt-terminal",
            "run_id": "run",
            "status": "completed",
            "duration_s": 60,
            "accepted_verdict": "pass",
        },
        {**common, "type": "verdict", "run_id": "run", "verdict": "pass"},
        {
            **common,
            "type": "review-launch-outcome-v1",
            "reservation_id": "rr_joined",
            "review_outcome": "consumed",
            "elapsed_seconds": 60,
            "cost_source": "unknown",
            "terminal_at": "2026-09-14T10:01:00+00:00",
        },
    ]

    totals = review_stats(rows).totals
    assert totals.starts == 1
    assert totals.terminals == 1
    assert totals.verdicts == 1
    assert totals.minutes == 1


def test_nonverdict_attempt_terminal_never_counts_as_a_verdict():
    common = {
        "task_id": "discovery-task",
        "attempt_index": 0,
        "attempt_id": "discovery-task:0",
        "review_intent": "discovery",
    }
    totals = review_stats(
        [
            {**common, "type": "review-nonverdict-launch-v1", "intent": "discovery"},
            {**common, "type": "attempt-start"},
            {**common, "type": "attempt-terminal", "status": "completed"},
            {**common, "type": "verdict", "verdict": "pass"},
        ]
    ).totals
    assert totals.starts == 1
    assert totals.terminals == 1
    assert totals.verdicts == 0
