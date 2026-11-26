"""The adjudicated startup failures never become publication or renewed budget."""

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from loopzero import cli, eligibility, runners
from tests import test_cli, test_runners
from tests.test_cli import expect_cli, posted, prepare_review
from tests.test_runners import (
    APPROVE, _fake_claude, _fake_codex, _native_codex_result, _review, replace_codex_events,
)

repo, wt, gh, head = test_cli.repo, test_cli.wt, test_cli.gh, test_cli.head
git_seed = test_cli.git_seed
fake_bwrap = test_runners.fake_bwrap

RECEIPTS = [row for size in ("five", "six") for row in json.loads(
    (Path(__file__).parent / f"fixtures/review-runtime-startup-{size}.json").read_text())]
PUBLISHER = ("example-review[bot]",)


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
def test_legacy_startup_failure_invalidates_receipts_and_older_approval(row, git_repo):
    bad = result(row)
    assert not runners.validate_runner_result(bad)
    cli._save_review(git_repo, bad)
    with pytest.raises(cli.CliError, match="provenance/envelope mismatch"):
        cli._load_review(git_repo, bad.head, bad.kind)
    clean = dataclasses.replace(bad, raw=json.dumps(envelope(row) | {"events": []}))
    with pytest.raises(runners.RunnerBadOutput, match="Code Mode"):
        runners.merge_reviews([clean, bad])
    older = copy.deepcopy(row)
    older["body"] = cli.review_marker(row["commit_id"], "primary") + "\n**approve**"
    assert eligibility.eligible_review([older, row], row["commit_id"], PUBLISHER) is None


@pytest.mark.parametrize("row", RECEIPTS, ids=lambda r: str(r["id"]))
@pytest.mark.parametrize("repost", [False, True], ids=["fresh-launch", "repost"])
def test_legacy_startup_never_publishes(wt, gh, fake_bin, capsys, row, repost):
    current = prepare_review(wt, gh, fake_bin)
    if repost:
        cli._save_review(wt, dataclasses.replace(result(row), head=current, kind="primary"))
        expect_cli(capsys, 1, "review", "--repost", stderr_contains=("provenance/envelope mismatch",))
        assert not (fake_bin / "codex.stdin").exists() and not (fake_bin / "claude.stdin").exists()
    else:
        # Fall through to the configured Codex route and inject the fatal native event.
        _fake_claude(fake_bin, "{}", exit_code=1)
        script = _fake_codex(fake_bin, json.dumps(APPROVE))
        replace_codex_events(script, [envelope(row)["events"][1]], prepend=True)
        expect_cli(capsys, 4, "review", stderr_contains=("Code Mode",))
        assert not list((wt / ".loopzero").glob("review-*.json"))
    assert not any(map(posted, gh.calls))


def test_review_budget_ignores_marker_for_a_different_commit():
    head = "a" * 40
    review = {"commit_id": head, "user": {"login": PUBLISHER[0]},
              "body": cli.review_marker("b" * 40, "primary") + "\n**approve**"}
    assert not cli._pr_markers([review], head, PUBLISHER)


@pytest.mark.parametrize(
    "mode", ["clean", "quoted", "path", "version", "after_tool", "unrelated_error", "quoted_error", "projected_quote"]
)
def test_only_structured_fatal_host_event_is_rejected(fake_bin, tmp_path, mode):
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
            "/opt/example/codex/0.164.0/bin/",
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
    markers = cli._pr_markers([review], row["commit_id"], PUBLISHER, repo="a/b", number=1)
    accepted = mode in ("clean", "quoted", "unrelated_error", "quoted_error", "projected_quote")
    assert bool(markers) is accepted
    script = _fake_codex(fake_bin, json.dumps(APPROVE))
    if mode not in ("clean", "projected_quote"):
        events = [{"type": "item.completed", "item": {
            "type": "command_execution", "status": "completed", "exit_code": 0,
        }}] if mode == "after_tool" else []
        replace_codex_events(script, [*events, fatal], prepend=True)
    if accepted:
        assert _review("codex", tmp_path).verdict == "approve"
    else:
        with pytest.raises(runners.RunnerBadOutput, match="Code Mode"):
            _review("codex", tmp_path)


# Keep the original routing inputs to catch a return of target-specific behavior.
@pytest.mark.parametrize(
    ("number", "case"), [(9179, 1), (9173, 4), (9174, 5), (9175, 6), (9275, 3), (9284, 2), (9220, 7)]
)
@pytest.mark.parametrize("repository", ["feder-positronics/intelflo", "elsewhere/intelflo"])
def test_retired_targets_use_ordinary_eligibility_and_budget(number, case, repository):
    target = "a" * 40
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == case])
    snapshot = copy.deepcopy(rows)
    replacement = {
        "id": 999, "commit_id": target, "user": {"login": PUBLISHER[0]},
        "state": "COMMENTED", "body": cli.review_marker(target, "primary") + "\n**approve**",
    }
    def markers(history):
        return cli._pr_markers(history, target, PUBLISHER, repo=repository, number=number)
    assert cli._decide_kind(markers(rows), target) == ("primary", None)
    assert eligibility.eligible_review(
        [*rows, replacement], target, PUBLISHER, repo=repository, number=number
    ) is replacement
    with pytest.raises(cli.CliError, match="already has a primary"):
        cli._decide_kind(markers([*rows, replacement]), target)
    delta = {**replacement, "id": 1000, "body": cli.review_marker(target, "delta") + "\n**approve**"}
    with pytest.raises(cli.CliError, match="review cap reached"):
        cli._decide_kind(markers([*rows, replacement, delta]), target)
    assert rows == snapshot


def test_duplicate_primary_markers_on_one_head_count_as_one():
    reviewed, head = "a" * 40, "b" * 40
    duplicate = [cli.Marker(reviewed, "primary", "COMMENTED")] * 2
    assert cli._decide_kind(duplicate, head) == ("delta", reviewed)
    with pytest.raises(cli.CliError, match="already has a primary"):
        cli._decide_kind(duplicate, reviewed)
    with pytest.raises(cli.CliError, match="review cap reached"):
        cli._decide_kind([*duplicate, cli.Marker("c" * 40, "primary", "COMMENTED")], head)
