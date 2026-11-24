import json
import subprocess

import pytest

from scripts import retro_metrics

REPO = "example/sample"
SINCE = "2026-05-28T00:00:00Z"
END = "2026-05-29T00:00:00Z"


def issue(number, created, closed=None, reason=None):
    return {"number": number, "created_at": created, "closed_at": closed, "state_reason": reason}


@pytest.fixture
def sources(tmp_path, monkeypatch):
    merged = issue(1, "2026-05-26T06:00:00Z", SINCE)
    at_end = issue(2, SINCE, END)
    p0 = issue(4, "2026-05-23T12:00:00Z")
    active = issue(5, "2026-05-25T12:00:00Z")
    pull_ref = {
        "event": "cross-referenced", "created_at": "2026-05-26T01:00:00Z",
        "source": {"issue": {"created_at": "2026-05-26T00:00:00Z", "pull_request": {
            "url": f"https://api.github.com/repos/{REPO}/pulls/3",
        }}},
    }
    rows = {
        "pulls": [dict(merged, merged_at=SINCE), dict(at_end, merged_at=END)],
        "outcomes": [
            issue(6, "2026-05-24T00:00:00Z", SINCE, "completed"),
            issue(7, "2026-05-24T00:00:00Z", SINCE, "not_planned"),
        ],
        "orders": [issue(8, "2026-05-27T00:00:00Z", SINCE, "completed")],
        "p0": [p0, active],
        "issues/4/timeline": [
            {"event": "labeled", "created_at": "2026-05-28T23:45:00Z"},
            {"event": "commented", "created_at": "2026-05-28T06:00:00Z"},
            {"event": "commented", "created_at": END},
        ],
        "issues/5/timeline": [pull_ref],
        "pulls/3/commits": [{"commit": {"committer": {"date": "2026-05-28T21:00:00Z"}}}],
        "issues/3/comments": [],
    }
    calls = []

    def collect(argv, *, input, **kwargs):
        calls.append(argv)
        if argv[0] == "host-adapter":
            request = json.loads(input)
            assert request["repo"] == REPO
            assert len(request["windows"]) == 5
            data = {
                "custom_window": {"owner_messages": 0, "tokens_by_role": {"builder": 31},
                                  "spend": 1.25},
                "owners": {"coordinator": False},
            }
        else:
            path, _, query = argv[4].removeprefix(f"repos/{REPO}/").partition("?")
            assert argv[:4] == ["gh", "api", "--method", "GET"]
            if path == "issues":
                label = query.split("labels=")[1].split("&")[0]
                if label not in rows:
                    raise subprocess.CalledProcessError(1, argv)
                path = label
            data = rows[path]
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    monkeypatch.setattr(retro_metrics.subprocess, "run", collect)
    ledger = tmp_path / "receipts.jsonl"
    base = {"event": "job", "command_class": "loopzero-check"}
    records = [
        dict(base, outcome="ok", exit_code=0, end_utc="2026-04-30T00:00:00Z"),
        dict(base, outcome="ok", exit_code=0, end_utc=SINCE),
        dict(base, outcome="ok", exit_code=0, end_utc=SINCE),
        dict(base, outcome="fail", exit_code=1, end_utc="2026-05-28T01:00:00Z"),
        dict(base, outcome="cancelled", exit_code=143, end_utc="2026-05-28T02:00:00Z"),
        dict(base, outcome="ok", exit_code=0, end_utc=END),
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in records))
    return calls, [
        "--repo", REPO, "--since", SINCE, "--until", END,
        "--label-outcome", "outcomes", "--label-order", "orders", "--label-p0", "p0",
        "--frozen-inputs", str(tmp_path), "--check-receipts", str(ledger),
    ]


def test_record_replay_and_weekly_window(sources, capsys):
    calls, args = sources
    assert retro_metrics.main([*args, "--record", "--host-command", "host-adapter"]) == 0
    live = capsys.readouterr()
    assert sum(call == ["host-adapter"] for call in calls) == 1
    assert retro_metrics.main(args) == 0
    assert capsys.readouterr() == live
    assert len(calls) == 5

    report = json.loads(live.out)
    row = report["custom_window"]
    assert row["merges"] == 1
    assert row["open_to_merge_median_hours"] == 42
    assert row["outcomes_closed"] == 1
    assert row["orders_accepted"] == 1
    assert row["order_to_acceptance_median_hours"] == 24
    assert (row["checks_passed"], row["checks_failed"]) == (1, 1)
    assert (row["open_p0"], row["oldest_open_p0_hours"]) == (2, 132)
    assert row["owner_messages"] == 0
    assert row["tokens_by_role"] == {"builder": 31}
    assert row["spend"] == 1.25
    assert len(report["trend_4_weeks"]) == 4
    assert report["trend_4_weeks"][0]["since"] == "2026-05-01T00:00:00Z"
    assert report["previous_7_days"]["until"] == report["last_7_days"]["since"]
    assert "Previous 7d" in live.err


def test_alerts_find_stalled_order_and_dead_owner(sources, capsys):
    calls, args = sources
    options = ["--alerts", "--stall-hours", "12", "--owner", "coordinator",
               "--band", "merges", "2", "3"]
    assert retro_metrics.main([*args, *options, "--record", "--host-command", "host-adapter"]) == 0
    live = capsys.readouterr()
    findings = json.loads(live.out)["alerts"]
    assert live.err == ""
    assert [finding for finding in findings if finding["kind"] != "unknown"] == [
        {"kind": "outside_band", "metric": "merges", "value": 1, "min": 2, "max": 3},
        {"kind": "stalled_p0", "issue": 4, "last_activity": "2026-05-28T06:00:00Z", "hours": 18},
        {"kind": "dead_owner", "owner": "coordinator"},
    ]
    count = len(calls)
    assert retro_metrics.main([*args, *options]) == 0
    assert capsys.readouterr() == live
    assert len(calls) == count


def test_missing_and_failed_sources_do_not_report_zero(sources, capsys):
    _, args = sources
    args[args.index("--label-order") + 1] = "missing"
    args[args.index("--check-receipts") + 1] = "/no-such-receipt-test-file"
    assert retro_metrics.main([*args, "--record"]) == 0
    report = json.loads(capsys.readouterr().out)
    for key in ("orders_accepted", "checks_passed", "checks_failed", *retro_metrics.HOST_KEYS):
        assert report["custom_window"][key] == "UNKNOWN"
        assert any(a["source"] == f"custom_window/{key}" for a in report["alerts"])
    assert report["custom_window"]["outcomes_closed"] == 1
    assert report["previous_7_days"]["outcomes_closed"] == 0


def test_invalid_band_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        retro_metrics.main(["--repo", REPO, "--until", END, "--band", "merges", "3", "2"])
    assert exc.value.code == 2
    assert "ordered limits" in capsys.readouterr().err
