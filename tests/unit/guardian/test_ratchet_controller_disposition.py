"""Fixture tests for scripts/util/guardian_tick.py (guardian system slice 1).

The tick is the deterministic dispatcher: quiet when all quality claims are
green or tracked, first VIOLATED claim (manifest order) is THE ticket,
broken sentinels are never ignorable.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(
    reason="(a) Guardian tick scheduling, recording, and launch controller stay product-side"
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "util" / "guardian_tick.py"
STATE_SCRIPT = REPO_ROOT / "scripts" / "util" / "guardian_state.py"


def load_state_module():
    spec = importlib.util.spec_from_file_location(
        "guardian_state_tick_test", STATE_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_manifest(tmp_path: Path, quality_entries: str) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text("claims: []\ndossier: []\nquality:\n" + quality_entries)
    return path


def run_tick(manifest: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest), *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )


GREEN = """
  - id: QT-green
    claim: value stays low
    type: metric_max
    command: echo 1
    threshold: 5
    headroom: 2
    repair_scope: [scripts/util/guardian_tick.py]
    acceptance_command: pytest guardian
    candidate_command: printf 'scripts/util/guardian_tick.py\\n'
    expected: verified
"""

VIOLATED_A = """
  - id: QT-first
    claim: first budget holds
    type: metric_max
    command: echo 9
    threshold: 5
    headroom: 2
    repair_scope: [scripts/util/guardian_tick.py]
    acceptance_command: pytest guardian
    candidate_command: printf 'scripts/util/guardian_tick.py\\n'
    expected: verified
"""

VIOLATED_B = """
  - id: QT-second
    claim: second budget holds
    type: metric_max
    command: echo 9
    threshold: 5
    headroom: 2
    repair_scope: [scripts/util/guardian_tick.py]
    acceptance_command: pytest guardian
    candidate_command: printf 'scripts/util/guardian_tick.py\\n'
    expected: verified
"""

TRACKED = """
  - id: QT-tracked
    claim: known debt is tracked
    type: metric_max
    command: echo 9
    threshold: 5
    headroom: 2
    repair_scope: [scripts/util/guardian_tick.py]
    acceptance_command: pytest guardian
    candidate_command: printf 'scripts/util/guardian_tick.py\\n'
    expected: known-gap
    tracked: "#999"
"""

FIXED = """
  - id: QT-fixed
    claim: gap that closed
    type: metric_max
    command: echo 1
    threshold: 5
    headroom: 2
    repair_scope: [scripts/util/guardian_tick.py]
    acceptance_command: pytest guardian
    candidate_command: printf 'scripts/util/guardian_tick.py\\n'
    expected: known-gap
    tracked: "#998"
"""

BROKEN = """
  - id: QT-broken
    claim: sentinel that cannot run
    type: metric_max
    command: exit 7
    threshold: 5
    headroom: 2
    repair_scope: [scripts/util/guardian_tick.py]
    acceptance_command: pytest guardian
    candidate_command: printf 'scripts/util/guardian_tick.py\\n'
    expected: verified
"""


def test_quiet_tick_exits_zero(tmp_path: Path) -> None:
    result = run_tick(write_manifest(tmp_path, GREEN))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "quiet tick" in result.stdout


def test_first_violated_claim_is_the_ticket(tmp_path: Path) -> None:
    result = run_tick(write_manifest(tmp_path, VIOLATED_A + VIOLATED_B))
    assert result.returncode == 3
    assert "🎫 ticket: QT-first" in result.stdout
    assert "QT-second" not in result.stdout.split("🎫")[1]
    assert "guardian-reference.md" in result.stdout


def test_tracked_claim_is_suppressed(tmp_path: Path) -> None:
    result = run_tick(write_manifest(tmp_path, TRACKED + GREEN))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 tracked" in result.stdout
    assert "ticket" not in result.stdout.split("—")[1].split("\n")[0]


def test_fixed_gap_reports_ratchet_flip_not_ticket(tmp_path: Path) -> None:
    result = run_tick(write_manifest(tmp_path, FIXED))
    assert result.returncode == 0
    assert "flip to expected: verified" in result.stdout


def test_broken_sentinel_exits_two(tmp_path: Path) -> None:
    result = run_tick(write_manifest(tmp_path, BROKEN + VIOLATED_A))
    assert result.returncode == 2
    assert "broken sentinel QT-broken" in result.stdout


def test_empty_or_malformed_manifest_is_structured_broken(tmp_path: Path) -> None:
    for index, body in enumerate(
        ("claims: []\ndossier: []\nquality: []\n", "quality: [\n")
    ):
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(body)
        result = run_tick(
            manifest,
            "--json",
            "--mode",
            "service",
            "--source-sha",
            "d" * 40,
            "--audit-dir",
            str(tmp_path / f"agent-events-{index}"),
        )
        assert result.returncode == 2, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["recorded_transition"] == "broken"
        assert payload["groups"]["UNSUPPORTED"][0]["id"] == "GUARDIAN-MANIFEST"


def test_missing_repair_authority_is_broken_before_ticket(tmp_path: Path) -> None:
    incomplete = """
  - id: QT-incomplete
    claim: missing repair authority
    type: metric_max
    command: echo 9
    threshold: 5
    headroom: 2
    expected: verified
"""
    result = run_tick(write_manifest(tmp_path, incomplete), "--json")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert "ticket" not in payload
    assert "requires non-empty" in payload["groups"]["UNSUPPORTED"][0]["detail"]


def test_invalid_expected_and_duplicate_ids_are_broken(tmp_path: Path) -> None:
    invalid_expected = VIOLATED_A.replace("expected: verified", "expected: typo")
    duplicate_ids = VIOLATED_A + VIOLATED_B.replace("QT-second", "QT-first")
    for entries in (invalid_expected, duplicate_ids):
        result = run_tick(write_manifest(tmp_path, entries), "--json")
        assert result.returncode == 2
        assert json.loads(result.stdout)["groups"]["UNSUPPORTED"]


def test_non_metric_quality_claim_is_broken_before_measurement(tmp_path: Path) -> None:
    non_metric = VIOLATED_A.replace("type: metric_max", "type: grep_present")
    result = run_tick(write_manifest(tmp_path, non_metric), "--json")
    assert result.returncode == 2
    unsupported = json.loads(result.stdout)["groups"]["UNSUPPORTED"]
    assert unsupported[0]["detail"] == "quality claim type must be metric_max"


def test_plain_service_storage_failure_never_prints_quiet(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked")
    result = run_tick(
        write_manifest(tmp_path, GREEN),
        "--mode",
        "service",
        "--source-sha",
        "f" * 40,
        "--audit-dir",
        str(blocked),
    )
    assert result.returncode == 2
    assert "recording failed" in result.stdout
    assert "quiet tick" not in result.stdout


def test_json_mode_carries_ticket_and_charter(tmp_path: Path) -> None:
    result = run_tick(write_manifest(tmp_path, VIOLATED_A), "--json")
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["ticket"]["id"] == "QT-first"
    assert "make reentry" in payload["charter"]
    assert "ONE bounded session" in payload["charter"]


def test_diagnostic_tick_never_records_or_needs_a_source_sha(tmp_path: Path) -> None:
    audit = tmp_path / "agent-events"
    result = run_tick(write_manifest(tmp_path, GREEN), "--audit-dir", str(audit))

    assert result.returncode == 0
    assert not audit.exists()


def test_service_quiet_tick_records_source_bound_event_idempotently(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "agent-events"
    manifest = write_manifest(tmp_path, GREEN)
    args = (
        "--json",
        "--mode",
        "service",
        "--source-sha",
        "a" * 40,
        "--now",
        "2026-08-11T06:10:00Z",
        "--audit-dir",
        str(audit),
    )
    first = run_tick(manifest, *args)
    second = run_tick(manifest, *args)

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    payload = json.loads(first.stdout)
    assert payload["recorded_transition"] == "quiet"
    assert payload["groups"]["VERIFIED"][0]["value"] == 1.0
    rows = [
        json.loads(line)
        for line in next(audit.glob("*.jsonl")).read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["measured_source_sha"] == "a" * 40
    assert rows[0]["threshold"] == 5
    assert rows[0]["headroom"] == 4.0
    assert rows[0]["policy_headroom"] == 2
    assert rows[0]["repair_scope"] == ["scripts/util/guardian_tick.py"]
    assert rows[0]["acceptance_command"] == "pytest guardian"
    assert rows[0]["candidate_command"].startswith("printf")
    assert rows[0]["quality_verdicts"] == [
        {"claim_id": "QT-green", "status": "VERIFIED", "threshold": 5, "value": 1.0}
    ]


def test_service_without_source_fails_closed_and_records_no_model_work(
    tmp_path: Path,
) -> None:
    result = run_tick(
        write_manifest(tmp_path, VIOLATED_A),
        "--json",
        "--mode",
        "service",
        "--audit-dir",
        str(tmp_path / "agent-events"),
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["recorded_transition"] == "broken"
    assert "ticket" not in payload


def test_qualification_violation_is_terminal_broken_without_ticket(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "agent-events"
    deposit = tmp_path / "qualification-broken.json"
    result = run_tick(
        write_manifest(tmp_path, VIOLATED_A),
        "--json",
        "--mode",
        "qualification",
        "--source-sha",
        "a" * 40,
        "--qualification-key",
        "qualification:2026-08-13:gate-b-" + "b" * 32,
        "--audit-dir",
        str(audit),
        "--qualification-deposit",
        str(deposit),
    )

    assert result.returncode == 2, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["recorded_transition"] == "broken"
    assert "ticket" not in payload
    assert "charter" not in payload
    assert "qualification refuses" in payload["record_error"]
    assert not audit.exists()
    rows = [json.loads(line) for line in deposit.read_text().splitlines()]
    assert [row["transition"] for row in rows] == ["broken"]


def test_qualification_quiet_observation_binds_operator_identity(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "agent-events"
    deposit = tmp_path / "qualification-quiet.json"
    identity = "qualification:2026-08-13:gate-b-" + "c" * 32
    result = run_tick(
        write_manifest(tmp_path, GREEN),
        "--json",
        "--mode",
        "qualification",
        "--source-sha",
        "d" * 40,
        "--qualification-key",
        identity,
        "--audit-dir",
        str(audit),
        "--qualification-deposit",
        str(deposit),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["recorded_transition"] == "quiet"
    assert not audit.exists()
    row = json.loads(deposit.read_text().strip())
    assert row["identity"] == identity


def test_service_tracked_and_closed_escalations_have_distinct_outcomes(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "agent-events"
    manifest = write_manifest(tmp_path, TRACKED)
    issues = tmp_path / "issues.json"
    issues.write_text('[{"number": 999, "state": "open"}]')
    args = (
        "--json",
        "--mode",
        "service",
        "--source-sha",
        "b" * 40,
        "--now",
        "2026-08-11T12:01:00Z",
        "--audit-dir",
        str(audit),
        "--github-escalations",
        str(issues),
    )
    tracked = run_tick(manifest, *args)
    assert tracked.returncode == 0
    assert json.loads(tracked.stdout)["recorded_transition"] == "tracked"

    issues.write_text('[{"number": 999, "state": "closed"}]')
    closed = run_tick(
        manifest,
        *args[:5],
        "--now",
        "2026-08-11T18:01:00Z",
        "--audit-dir",
        str(audit),
        "--github-escalations",
        str(issues),
    )
    assert closed.returncode == 3
    closed_payload = json.loads(closed.stdout)
    assert closed_payload["recorded_transition"] == "ticket"
    assert closed_payload["ticket"]["id"] == "QT-tracked"


def test_prior_escalated_observation_suppresses_violated_claim_until_issue_closes(
    tmp_path: Path,
) -> None:
    import yaml

    audit = tmp_path / "agent-events"
    manifest = write_manifest(tmp_path, VIOLATED_A)
    claim = yaml.safe_load(manifest.read_text())["quality"][0]
    state = load_state_module()
    escalated = state.build_observation(
        identity="2026-08-11T06:00Z",
        claim=claim,
        measured_source_sha="c" * 40,
        transition="escalated",
        value=9.0,
        issue_number=4242,
    )
    assert state.append_observation(escalated, audit)
    issues = tmp_path / "issues.json"
    issues.write_text('[{"number": 4242, "state": "open"}]')

    common = (
        "--json",
        "--mode",
        "service",
        "--source-sha",
        "c" * 40,
        "--audit-dir",
        str(audit),
        "--github-escalations",
        str(issues),
    )
    tracked = run_tick(manifest, *common, "--now", "2026-08-11T12:20:00Z")
    assert tracked.returncode == 0, tracked.stdout + tracked.stderr
    assert json.loads(tracked.stdout)["recorded_transition"] == "tracked"
    assert "ticket" not in json.loads(tracked.stdout)

    issues.write_text('[{"number": 4242, "state": "closed"}]')
    eligible = run_tick(manifest, *common, "--now", "2026-08-11T18:20:00Z")
    assert eligible.returncode == 3, eligible.stdout + eligible.stderr
    assert json.loads(eligible.stdout)["recorded_transition"] == "ticket"


def test_reenabled_earlier_claim_preserves_manifest_priority(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, TRACKED + VIOLATED_B)
    issues = tmp_path / "issues.json"
    issues.write_text('[{"number": 999, "state": "closed"}]')
    result = run_tick(
        manifest,
        "--json",
        "--mode",
        "service",
        "--source-sha",
        "e" * 40,
        "--now",
        "2026-08-11T18:05:00Z",
        "--audit-dir",
        str(tmp_path / "agent-events"),
        "--github-escalations",
        str(issues),
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert json.loads(result.stdout)["ticket"]["id"] == "QT-tracked"
    ticket = json.loads(result.stdout)["ticket"]
    assert len(ticket["evaluation_id"]) == 64
    assert ticket["identity"] == "2026-08-11T18:00Z"
    assert ticket["measured_source_sha"] == "e" * 40
