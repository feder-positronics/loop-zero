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
    rows = {
        "pulls": [dict(issue(n, created, closed), merged_at=closed)
                  for n, created, closed in [(1, "2026-05-26T06:00:00Z", SINCE), (2, SINCE, END)]],
        "outcomes": [issue(n, "2026-05-24T00:00:00Z", SINCE, reason)
                     for n, reason in [(6, "completed"), (7, "not_planned")]],
        "orders": [issue(8, "2026-05-27T00:00:00Z", SINCE, "completed")],
        "orders_open": [issue(9, "2026-01-01T00:00:00Z")],
        "p0": [issue(4, "2026-05-23T12:00:00Z"), issue(5, "2026-05-25T12:00:00Z")],
        "issues/4/timeline": [{"event": event, "created_at": time} for event, time in [
            ("labeled", "2026-05-28T23:45:00Z"), ("commented", "2026-05-28T06:00:00Z"), ("commented", END),
        ]],
        "issues/5/timeline": [{
            "event": "cross-referenced", "created_at": "2026-05-26T01:00:00Z",
            "source": {"issue": {"created_at": "2026-05-26T00:00:00Z",
                                 "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/3"}}},
        }],
        "pulls/3": {"commits": commit_total},
        "pulls/3/commits": [{"commit": {"committer": {"date": "2026-05-28T21:00:00Z"}}}] * commit_rows,
        "issues/3/comments": [],
    }
    if commit_rows == 0:
        rows["p0"] = [issue(1, SINCE)]
        if commit_total is None:
            del rows["orders"]
            rows["orders_open"] = []
    calls = []

    def collect(argv, *, input, **kwargs):
        calls.append(argv)
        if argv[0] == "host-adapter":
            request = json.loads(input)
            assert request["repo"] == REPO
            assert len(request["windows"]) == 5
            data = {
                "custom_window": {"owner_messages": 0, "tokens_by_role": {"builder": 31}, "spend": 1.25},
                "owners": {"coordinator": False},
            }
        else:
            path, _, query = argv[4].removeprefix(f"repos/{REPO}/").partition("?")
            assert argv[:4] == ["gh", "api", "--method", "GET"]
            if path == "issues":
                path = query.split("labels=")[1].split("&")[0]
                if path == "orders" and "state=open" in query:
                    path = "orders_open"
            if path not in rows:
                raise subprocess.CalledProcessError(1, argv)
            data = rows[path]
            if path == "pulls/3/commits":
                offset = (int(query.rsplit("=", 1)[1]) - 1) * 100
                data = data[offset:offset + 100]
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    monkeypatch.setattr(retro_metrics.subprocess, "run", collect)
    ledger = tmp_path / "receipts.jsonl"
    base = {"event": "job", "command_class": "loopzero-check"}
    outcomes = [("ok", 0, when) for when in ("2026-04-30T00:00:00Z", SINCE, SINCE, END)]
    outcomes += [("fail", 1, "2026-05-28T01:00:00Z"), ("cancelled", 143, "2026-05-28T02:00:00Z")]
    ledger.write_text("".join(json.dumps(dict(base, outcome=outcome, exit_code=code, end_utc=end)) + "\n"
                              for outcome, code, end in outcomes))
    return calls, ["--repo", REPO, "--since", SINCE, "--until", END,
                   "--label-outcome", "outcomes", "--label-order", "orders", "--label-p0", "p0",
                   "--frozen-inputs", str(tmp_path), "--check-receipts", str(ledger)]


@pytest.fixture
def record(sources, capsys):
    calls, args = sources

    def run(*options, host=False, replay=True):
        command = [*args, *options]
        adapter = ["--host-command", "host-adapter"] if host else []
        assert retro_metrics.main([*command, "--record", *adapter]) == 0
        live, count = capsys.readouterr(), len(calls)
        if replay:
            assert retro_metrics.main(command) == 0
            assert capsys.readouterr() == live and len(calls) == count
        return json.loads(live.out), live.err

    return run


def test_record_replay_and_weekly_window(sources, record):
    calls, _ = sources
    report, stderr = record(host=True)
    assert sum(call == ["host-adapter"] for call in calls) == 1 and len(calls) == 6
    expected = dict(merges=1, open_to_merge_median_hours=42, outcomes_closed=1, orders_accepted=1,
                    order_to_acceptance_median_hours=24, checks_passed=1, checks_failed=1,
                    open_p0=2, oldest_open_p0_hours=132, owner_messages=0, tokens_by_role={"builder": 31}, spend=1.25)
    assert {key: report["custom_window"][key] for key in expected} == expected
    assert len(report["trend_4_weeks"]) == 4
    assert report["trend_4_weeks"][0]["since"] == "2026-05-01T00:00:00Z"
    assert report["previous_7_days"]["until"] == report["last_7_days"]["since"]
    assert report["weekly_sample"]["items"] == [
        {"kind": "merged_control", "number": 1},
        {"kind": "slowest_accepted", "number": 8},
        {"kind": "oldest_open_order", "number": 9},
    ]
    assert report["weekly_sample"]["shortfall"] == 3
    assert "Previous 7d" in stderr


def test_weekly_sample_reserves_controls_and_deduplicates_stalled_order():
    start, end = map(retro_metrics.timestamp, (SINCE, END))
    pulls = [{"number": number, "merged_at": SINCE} for number in range(10, 15)]
    orders = [issue(number, f"2026-05-{20 + number}T00:00:00Z") for number in range(1, 6)]
    orders += [issue(number, f"2026-05-{32 - number}T00:00:00Z", SINCE, "completed") for number in range(6, 9)]
    alerts = [{"kind": "stalled_p0", "issue": 1, "hours": 48}]
    sample = retro_metrics.weekly_sample(REPO, start, end, pulls, orders, orders, alerts)
    assert sample == retro_metrics.weekly_sample(
        REPO, start, end, pulls[::-1], orders[::-1], orders[::-1], alerts)
    assert len(sample["items"]) == 8
    assert sample["shortfall"] == 0
    assert all(item["kind"] == "merged_control" for item in sample["items"][:2])
    assert [item["number"] for item in sample["items"][2:]] == [8, 7, 1, 2, 3, 4]


@pytest.mark.parametrize("alerts", [False, True])
def test_stall_and_dead_owner_modes_record_and_replay(record, alerts):
    options = (["--alerts", "--owner", "coordinator", "--band", "merges", "2", "3"] if alerts
               else ["--dead-owner", "coordinator"])
    report, stderr = record("--stall-hours", "12", *options, host=alerts)
    findings = [item for item in report["alerts"] if item["kind"] != "unknown"]
    if alerts:
        assert stderr == "" and findings == [
            {"kind": "outside_band", "metric": "merges", "value": 1, "min": 2, "max": 3},
            {"kind": "stalled_p0", "issue": 4, "last_activity": "2026-05-28T06:00:00Z", "hours": 18},
            {"kind": "dead_owner", "owner": "coordinator"},
        ]
    else:
        assert not findings and {"kind": "stalled_p0", "number": 4} in report["weekly_sample"]["items"]


@pytest.mark.parametrize("sources", [(0, None)], indirect=True)
def test_missing_and_failed_sources_do_not_report_zero(sources, record, capsys, tmp_path):
    calls, args = sources
    assert retro_metrics.main(args) == 1 and calls == []
    assert not (missing := capsys.readouterr()).out and "no readable snapshot" in missing.err and "--record" in missing.err
    args[args.index("--check-receipts") + 1] = "/no-such-receipt-test-file"
    report, _ = record("--stall-hours", "1", replay=False)
    assert all(report["weekly_sample"]["pool_sizes"][kind] == "UNKNOWN" for kind in ("stalled_p0", "oldest_open_order"))
    assert any(a.get("source") == "p0/#1/activity" for a in report["alerts"])
    for key in ("orders_accepted", "checks_passed", "checks_failed", *retro_metrics.HOST_KEYS):
        assert report["custom_window"][key] == "UNKNOWN"
        assert any(a["source"] == f"custom_window/{key}" for a in report["alerts"])
    assert [report[window]["outcomes_closed"] for window in ("custom_window", "previous_7_days")] == [1, 0]
    assert retro_metrics.main([*args, "--record", "--frozen-inputs", str(tmp_path / "inputs.json/child")]) == 1
    assert "inputs.json/child" in capsys.readouterr().err


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
def test_stalled_issue_checks_linked_pr_commit_count_at_limit(sources, detail_reads, stalled, record):
    calls, _ = sources
    report, _ = record("--alerts", "--stall-hours", "1")
    alerts = report["alerts"]
    assert any(a["kind"] == "stalled_p0" and a["issue"] == 5 for a in alerts) == stalled
    assert any(a.get("source") == "p0/#5/activity" for a in alerts) == (not stalled)
    assert sum(call[-1] == f"repos/{REPO}/pulls/3" for call in calls) == detail_reads
