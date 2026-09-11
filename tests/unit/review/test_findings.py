from types import SimpleNamespace

import pytest

from loopzero.review import findings


def _configure(tmp_path):
    findings.configure(
        SimpleNamespace(
            root=tmp_path,
            audit_root=tmp_path.relative_to(tmp_path),
            finding_severities=("critical", "important", "suggestion"),
        )
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


def test_capture_replays_and_merge_expires_the_pr(tmp_path):
    _configure(tmp_path)
    request = _request(17)
    receipt = findings.capture_finding_records(tmp_path, request=request)
    assert findings.capture_finding_records(tmp_path, request=request) == receipt
    assert findings.replay_finding_capture(tmp_path, request=request) == receipt

    findings.expire_at_merge(tmp_path, pr=17, merge_sha="a" * 40)
    assert findings.load_finding_records(tmp_path, pr=17) == []
    with pytest.raises(findings.LedgerConflict, match="expire"):
        findings.capture_finding_records(tmp_path, request=_request(17, "review-2"))


def test_waiver_requires_authority_and_rationale(tmp_path):
    _configure(tmp_path)
    receipt = findings.capture_finding_records(tmp_path, request=_request(17))
    finding_id = receipt["finding_ids"][0]
    with pytest.raises(findings.LedgerConflict, match="authority"):
        findings.append_finding_transition(
            tmp_path, pr=17, finding_id=finding_id, status="waived",
            authority="", rationale="",
        )
    row = findings.append_finding_transition(
        tmp_path, pr=17, finding_id=finding_id, status="waived",
        authority="maintainer", rationale="accepted risk",
    )
    assert row["status"] == "waived"
    assert findings.count_live_important(tmp_path, pr=17) == 0


def test_pr_number_is_mandatory(tmp_path):
    _configure(tmp_path)
    with pytest.raises(findings.LedgerConflict, match="positive PR"):
        findings.build_finding_capture_request(
            producer_id="review", findings=[{"severity": "important", "claim": "x"}]
        )
