"""The adjudicated startup failures never become publication or renewed budget."""

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from loopzero import cli, eligibility, runners
from tests import test_cli, test_runners
from tests.test_cli import post_key, prepare_review, run
from tests.test_runners import APPROVE, _fake_claude, _fake_codex, _native_codex_result, _review

repo, wt, gh, head = test_cli.repo, test_cli.wt, test_cli.gh, test_cli.head
fake_bwrap = test_runners.fake_bwrap

RECEIPTS = [row for date in ("20261027", "20261028") for row in json.loads(
    (Path(__file__).parent / f"fixtures/review-runtime-{date}.json").read_text())]
PUBLISHER = ("loopzero-feder-positronics[bot]",)


def envelope(row):
    return (
        json.loads(row["_raw"])
        if "_raw" in row
        else json.loads(row["body"].split("```")[1].strip())
    )


def result(row):
    raw = envelope(row)
    parsed = runners.parse_review(
        json.loads(raw["final_message"]),
        family="codex",
        head=row["commit_id"],
        kind="primary",
        raw=json.dumps(raw),
    )
    provenance = {
        "family": "codex",
        "session_id": raw["session_id"],
        "session_ids": [raw["session_id"]],
        "token_usage": raw["token_usage"],
    }
    if "review_context" in raw:
        provenance["requested_settings"] = [raw["review_context"]["requested"]]
    return dataclasses.replace(parsed, provenance=provenance)


@pytest.mark.parametrize("row", RECEIPTS, ids=lambda r: str(r["id"]))
def test_real_startup_failure_refuses_saved_result_and_multichunk(row, tmp_path):
    bad = result(row)
    assert not runners.validate_runner_result(bad)
    cli._save_review(tmp_path, bad)
    with pytest.raises(cli.CliError, match="provenance/envelope mismatch"):
        cli._load_review(tmp_path, bad.head, bad.kind)
    clean = dataclasses.replace(bad, raw=json.dumps(envelope(row) | {"events": []}))
    with pytest.raises(runners.RunnerBadOutput, match="Code Mode"):
        runners.merge_reviews([clean, bad])


@pytest.mark.parametrize("row", RECEIPTS, ids=lambda r: str(r["id"]))
def test_real_startup_exit_zero_never_publishes(wt, gh, fake_bin, capsys, row):
    prepare_review(wt, gh, fake_bin)
    # Force the already-configured Codex route without changing the public CLI policy.

    _fake_claude(fake_bin, "{}", exit_code=1)
    raw = envelope(row)
    script = _fake_codex(fake_bin, json.dumps(APPROVE))
    script.write_text(
        script.read_text().replace(
            'echo \'{"type":"thread.started"',
            "printf '%s\\n' '"
            + json.dumps(raw["events"][1])
            + '\'\necho \'{"type":"thread.started"',
        )
    )
    code, _, error = run(capsys, "review")
    assert code == 4 and "Code Mode" in error
    assert not any(c["argv"][:2] == post_key().split() for c in gh.calls)
    assert not list((wt / ".loopzero").glob("review-*.json"))


@pytest.mark.parametrize(
    "mode", ["clean", "quoted", "path", "version", "after_tool", "unrelated_error", "quoted_error", "projected_quote"]
)
def test_only_structured_fatal_host_event_is_rejected(fake_bin, tmp_path, monkeypatch, mode):
    row = copy.deepcopy(RECEIPTS[0])
    raw = envelope(row)
    fatal = raw["events"][1]
    if mode == "quoted":
        fatal["item"]["type"] = "agent_message"
    if mode == "unrelated_error":
        fatal["item"]["message"] = "Another nonfatal warning"
    if mode == "quoted_error":
        fatal["item"]["message"] = "Quoted evidence: " + fatal["item"]["message"]
    if mode == "path":
        fatal["item"]["message"] = fatal["item"]["message"].replace(
            "/home/marcin/.codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl/bin/",
            "/another release/bin/",
        )
    if mode == "version":
        fatal["item"]["message"] = fatal["item"]["message"].replace("0.164.0", "9.999.1")
    native = raw if mode != "clean" else {**raw, "events": []}
    body = cli.review_marker(row["commit_id"], "primary") + "\n<details><summary>Raw reviewer output</summary>\n```\n" + json.dumps(native) + "\n```\n</details>"
    if mode == "projected_quote":
        clean = _native_codex_result(row["commit_id"], repo="a/b", number=1, publisher=PUBLISHER[0])
        body = body.replace("\n", "\n" + runners.encode_codex_evidence(runners.codex_evidence(clean)) + "\n\n", 1)
    review = {**row, "body": body, "state": "APPROVED"}
    if mode == "projected_quote":
        assert eligibility.eligible_review([review], row["commit_id"], PUBLISHER, repo="a/b", number=1)
    monkeypatch.setattr(cli, "_is_ancestor", lambda *a: True)
    markers = cli._lineage_markers(tmp_path, [review], row["commit_id"], PUBLISHER, repo="a/b", number=1)
    assert bool(markers) == (mode in ("clean", "quoted", "unrelated_error", "quoted_error", "projected_quote"))
    script = _fake_codex(fake_bin, json.dumps(APPROVE))
    if mode == "after_tool":
        tool = {
            "type": "item.completed",
            "item": {"type": "command_execution", "status": "completed", "exit_code": 0},
        }
        script.write_text(
            script.read_text().replace(
                'echo \'{"type":"thread.started"',
                "printf '%s\\n' '" + json.dumps(tool) + '\'\necho \'{"type":"thread.started"',
            )
        )
    if mode not in ("clean", "projected_quote"):
        script.write_text(
            script.read_text().replace(
                'echo \'{"type":"thread.started"',
                "printf '%s\\n' '" + json.dumps(fatal) + '\'\necho \'{"type":"thread.started"',
            )
        )
    if mode in ("clean", "quoted", "unrelated_error", "quoted_error", "projected_quote"):
        assert _review("codex", tmp_path).verdict == "approve"
    else:
        with pytest.raises(runners.RunnerBadOutput, match="Code Mode"):
            _review("codex", tmp_path)


@pytest.mark.parametrize("number", [9179, 9173, 9174, 9175, 9275, 9284])
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "repo",
        "number",
        "publisher",
        "body",
        "raw",
        "head",
        "target",
        "id",
        "missing",
        "duplicate",
    ],
)
def test_retired_incidents_never_grant_recovery(number, change):
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == number])
    repo = "feder-positronics/intelflo"
    target = next((r["commit_id"] for r in reversed(rows)), "e" * 40)
    selected = number
    if change == "repo":
        repo = "elsewhere/intelflo"
    if change == "number":
        selected = 1
    if change == "publisher":
        rows[0]["user"]["login"] = "stranger"
    if change == "body":
        rows[0]["body"] += " "
    if change == "raw":
        rows[0]["body"] = rows[0]["body"].replace("codex_exec_result", "codex_exec_resulT")
    if change == "head":
        rows[0]["commit_id"] = "f" * 40
    if change == "target":
        target = "e" * 40
    if change == "id":
        rows[0]["id"] += 1
    if change == "missing":
        rows = []
    if change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    found = eligibility.recovery_reviews(repo, selected, target, rows)
    assert found == ()
    assert eligibility.recovery_target(repo, selected) is None
    assert eligibility.recovery_suffix(repo, selected, target, rows) == ""
    assert eligibility.recovery_baseline(selected, rows)
    assert eligibility._RECOVERY_TARGETS == {}
    assert eligibility._RECOVERY_RECEIPTS == {}


@pytest.mark.parametrize("row", RECEIPTS, ids=lambda r: str(r["id"]))
def test_invalid_latest_cannot_revive_older_approval(row):
    older = copy.deepcopy(row)
    older["body"] = cli.review_marker(row["commit_id"], "primary") + "\n**approve**"
    assert eligibility.eligible_review([older, row], row["commit_id"], PUBLISHER) is None


@pytest.mark.parametrize("row", RECEIPTS, ids=lambda r: str(r["id"]))
def test_real_startup_repost_refuses_before_any_publication(wt, gh, fake_bin, capsys, row):
    current = prepare_review(wt, gh, fake_bin)
    bad = dataclasses.replace(result(row), head=current, kind="primary")
    cli._save_review(wt, bad)
    code, _, error = run(capsys, "review", "--repost")
    assert code == 1 and "provenance/envelope mismatch" in error
    assert not any(c["argv"][:2] == post_key().split() for c in gh.calls)
    assert not (fake_bin / "codex.stdin").exists() and not (fake_bin / "claude.stdin").exists()



@pytest.mark.parametrize("number", [9179, 9173, 9174, 9175, 9275, 9284, 9220])
@pytest.mark.parametrize("repository", ["feder-positronics/intelflo", "elsewhere/intelflo"])
def test_retired_targets_use_ordinary_eligibility_and_budget(tmp_path, monkeypatch, number, repository):
    target = "a" * 40
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == number])
    snapshot = copy.deepcopy(rows)
    replacement = {
        "id": 999, "commit_id": target, "user": {"login": PUBLISHER[0]},
        "state": "COMMENTED", "body": cli.review_marker(target, "primary") + "\n**approve**",
    }
    monkeypatch.setattr(cli, "_is_ancestor", lambda *a: True)
    def markers(history):
        return cli._lineage_markers(tmp_path, history, target, PUBLISHER, repo=repository, number=number)
    assert cli._decide_kind(markers(rows), target) == ("primary", None)
    assert eligibility.eligible_review(
        [*rows, replacement], target, PUBLISHER, repo=repository, number=number
    ) is replacement
    with pytest.raises(cli.CliError, match="already has a primary"):
        cli._decide_kind(markers([*rows, replacement]), target)
    delta = {**replacement, "id": 1000, "body": cli.review_marker(target, "delta") + "\n**approve**"}
    with pytest.raises(cli.CliError, match="budget exhausted"):
        cli._decide_kind(markers([*rows, replacement, delta]), target)
    assert rows == snapshot
