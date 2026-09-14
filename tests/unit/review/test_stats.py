import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loopzero.kernel.authority_store import retained_authority_projection_once
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


def test_pre_release_ledger_uses_unrecorded_and_unknown_not_zero():
    rows = [
        {
            "type": "attempt-start",
            "task_id": "old-review",
            "attempt_index": 0,
            "run_id": "old-run",
            "review_intent": "delivery-code-review",
        },
        {
            "type": "attempt-terminal",
            "task_id": "old-review",
            "attempt_index": 0,
            "run_id": "old-run",
            "review_intent": "delivery-code-review",
            "status": "completed",
            "reason": "legacy-terminal-label",
        },
    ]
    assert retained_authority_projection_once(rows) == rows
    result = review_stats(rows).to_dict()
    assert result["per_pr"].keys() == {"unrecorded"}
    assert result["per_reason"].keys() == {"unrecorded"}
    assert result["per_engine"].keys() == {"unrecorded"}
    assert result["totals"]["claim_counts_unrecorded"] == 1
    assert result["totals"]["api_equivalent_usd"] is None
    assert result["totals"]["cost_unknown"] == 1
