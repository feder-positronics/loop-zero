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
def sources(tmp_path, monkeypatch, request):
    commit_rows, commit_total = getattr(request, "param", (1, 1))
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
        "orders_open": [issue(9, "2026-01-01T00:00:00Z")],
        "p0": [p0, active],
        "issues/4/timeline": [
            {"event": "labeled", "created_at": "2026-05-28T23:45:00Z"},
            {"event": "commented", "created_at": "2026-05-28T06:00:00Z"},
            {"event": "commented", "created_at": END},
        ],
        "issues/5/timeline": [pull_ref],
        "pulls/3": {"commits": commit_total},
        "pulls/3/commits": [{"commit": {"committer": {"date": "2026-05-28T21:00:00Z"}}}] * commit_rows,
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
                if label == "orders" and "state=open" in query:
                    label = "orders_open"
                if label not in rows:
                    raise subprocess.CalledProcessError(1, argv)
                path = label
            data = rows[path]
            if path == "pulls/3/commits":
                offset = (int(query.rsplit("=", 1)[1]) - 1) * 100
                data = data[offset:offset + 100]
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    monkeypatch.setattr(retro_metrics.subprocess, "run", collect)
    ledger = tmp_path / "receipts.jsonl"
    base = {"event": "job", "command_class": "loopzero-check"}
    outcomes = [
        ("ok", 0, "2026-04-30T00:00:00Z"), ("ok", 0, SINCE), ("ok", 0, SINCE),
        ("fail", 1, "2026-05-28T01:00:00Z"), ("cancelled", 143, "2026-05-28T02:00:00Z"),
        ("ok", 0, END),
    ]
    ledger.write_text("".join(json.dumps(dict(base, outcome=outcome, exit_code=code, end_utc=end)) + "\n"
                              for outcome, code, end in outcomes))
    return calls, [
        "--repo", REPO, "--since", SINCE, "--until", END,
        "--label-outcome", "outcomes", "--label-order", "orders", "--label-p0", "p0",
        "--frozen-inputs", str(tmp_path), "--check-receipts", str(ledger),
    ]


def recorded_output(capsys, args, *options):
    assert retro_metrics.main([*args, *options, "--record"]) == 0
    return capsys.readouterr()


def test_record_replay_and_weekly_window(sources, capsys):
    calls, args = sources
    live = recorded_output(capsys, args, "--host-command", "host-adapter")
    assert sum(call == ["host-adapter"] for call in calls) == 1
    assert len(calls) == 6
    assert_replay(args, calls, capsys, live)

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
    assert report["weekly_sample"]["items"] == [
        {"kind": "merged_control", "number": 1},
        {"kind": "slowest_accepted", "number": 8},
        {"kind": "oldest_open_order", "number": 9},
    ]
    assert report["weekly_sample"]["shortfall"] == 3
    assert "Previous 7d" in live.err


def test_weekly_sample_reserves_controls_and_deduplicates_stalled_order():
    start, end = map(retro_metrics.timestamp, (SINCE, END))
    pulls = [{"number": number, "merged_at": SINCE} for number in range(10, 15)]
    orders = [
        issue(1, "2026-05-21T00:00:00Z"),
        issue(2, "2026-05-22T00:00:00Z"),
        issue(3, "2026-05-23T00:00:00Z"),
        issue(4, "2026-05-24T00:00:00Z"),
        issue(5, "2026-05-25T00:00:00Z"),
        issue(6, "2026-05-26T00:00:00Z", SINCE, "completed"),
        issue(7, "2026-05-25T00:00:00Z", SINCE, "completed"),
        issue(8, "2026-05-24T00:00:00Z", SINCE, "completed"),
    ]
    alerts = [{"kind": "stalled_p0", "issue": 1, "hours": 48}]
    sample = retro_metrics.weekly_sample(REPO, start, end, pulls, orders, orders, alerts)
    assert sample == retro_metrics.weekly_sample(
        REPO, start, end, pulls[::-1], orders[::-1], orders[::-1], alerts)
    assert len(sample["items"]) == 8
    assert sample["shortfall"] == 0
    assert all(item["kind"] == "merged_control" for item in sample["items"][:2])
    assert [item["number"] for item in sample["items"][2:]] == [8, 7, 1, 2, 3, 4]


def assert_replay(args, calls, capsys, live):
    count = len(calls)
    assert retro_metrics.main(args) == 0
    assert capsys.readouterr() == live
    assert len(calls) == count


def test_weekly_report_uses_stall_threshold_without_running_other_alerts(sources, capsys):
    calls, args = sources
    args = [*args, "--stall-hours", "12", "--dead-owner", "coordinator"]
    live = recorded_output(capsys, args)
    report = json.loads(live.out)
    assert {"kind": "stalled_p0", "number": 4} in report["weekly_sample"]["items"]
    assert all(alert["kind"] == "unknown" for alert in report["alerts"])
    assert_replay(args, calls, capsys, live)


def test_alerts_find_stalled_order_and_dead_owner(sources, capsys):
    calls, args = sources
    options = ["--alerts", "--stall-hours", "12", "--owner", "coordinator",
               "--band", "merges", "2", "3"]
    live = recorded_output(capsys, args, *options, "--host-command", "host-adapter")
    findings = json.loads(live.out)["alerts"]
    assert live.err == ""
    assert [finding for finding in findings if finding["kind"] != "unknown"] == [
        {"kind": "outside_band", "metric": "merges", "value": 1, "min": 2, "max": 3},
        {"kind": "stalled_p0", "issue": 4, "last_activity": "2026-05-28T06:00:00Z", "hours": 18},
        {"kind": "dead_owner", "owner": "coordinator"},
    ]
    assert_replay([*args, *options], calls, capsys, live)


def test_missing_and_failed_sources_do_not_report_zero(sources, capsys):
    _, args = sources
    args[args.index("--label-order") + 1] = "missing"
    args[args.index("--check-receipts") + 1] = "/no-such-receipt-test-file"
    report = json.loads(recorded_output(capsys, args).out)
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


@pytest.mark.parametrize(("sources", "detail_reads", "stalled"), [
    ((101, None), 0, True),
    ((250, 250), 1, True),
    ((250, 251), 1, False),
    ((250, None), 1, False),
], indirect=["sources"])
def test_stalled_issue_checks_linked_pr_commit_count_at_limit(sources, detail_reads, stalled, capsys):
    calls, args = sources
    options = ["--alerts", "--stall-hours", "1"]
    live = recorded_output(capsys, args, *options)
    alerts = json.loads(live.out)["alerts"]
    assert any(a["kind"] == "stalled_p0" and a["issue"] == 5 for a in alerts) == stalled
    assert any(a.get("source") == "p0/#5/activity" for a in alerts) == (not stalled)
    assert sum(call[-1] == f"repos/{REPO}/pulls/3" for call in calls) == detail_reads

    assert_replay([*args, *options], calls, capsys, live)
