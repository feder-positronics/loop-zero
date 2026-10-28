"""The adjudicated startup failures never become publication or renewed budget."""

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from loopzero import cli, eligibility, github, runners
from tests import test_cli, test_runners
from tests.test_cli import post_key, prepare_review, run
from tests.test_runners import APPROVE, _fake_claude, _fake_codex, _native_codex_result, _review

repo, wt, gh, head = test_cli.repo, test_cli.wt, test_cli.gh, test_cli.head
fake_bwrap = test_runners.fake_bwrap

RECEIPTS = [row for date in ("20261027", "20261028") for row in json.loads(
    (Path(__file__).parent / f"fixtures/review-runtime-{date}.json").read_text())]
TARGETS = {
    9173: "f07d09b764c0a8c06929bf0f02b877a58a4c1ca4",
    9174: "c3e1e2f2fcfbdac413aaa3b9388e8837bfe1be9b",
    9175: "6e80b6ce590cc308792b914d8f02f5416ef77765",
    9179: "f3e8d79d49214163a395d760144002048c3c564f",
    9284: "1b836da3a29e31cb08a9fab19f0f567711e7ab4a",
    9275: "aab1b62bb242f62da4ceebcb593b0725942074e0",
}
PUBLISHER = ("loopzero-feder-positronics[bot]",)


def eligible_recovery(reviews, number, target):
    return eligibility.eligible_review(
        reviews, target, PUBLISHER, repo="feder-positronics/intelflo", number=number
    )


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


def publish_replacement(row, raw):
    row["_raw"] = json.dumps(raw)
    native = result(row)
    prefix = cli.review_marker(row["commit_id"], "primary") + eligibility.recovery_suffix(
        row["repo"], row["number"], row["commit_id"], RECEIPTS
    )
    row["body"] = github._review_body(native, [], prefix)
    return row


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
    "mode", ["clean", "quoted", "path", "version", "after_tool", "unrelated_error", "quoted_error"]
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
            "/home/marcin/.codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl/bin/",
            "/another release/bin/",
        )
    if mode == "version":
        fatal["item"]["message"] = fatal["item"]["message"].replace("0.164.0", "9.999.1")
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
    if mode != "clean":
        script.write_text(
            script.read_text().replace(
                'echo \'{"type":"thread.started"',
                "printf '%s\\n' '" + json.dumps(fatal) + '\'\necho \'{"type":"thread.started"',
            )
        )
    if mode in ("clean", "quoted", "unrelated_error", "quoted_error"):
        assert _review("codex", tmp_path).verdict == "approve"
    else:
        with pytest.raises(runners.RunnerBadOutput, match="Code Mode"):
            _review("codex", tmp_path)


@pytest.mark.parametrize("number", TARGETS)
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
def test_adjudication_matches_complete_pinned_incident_only(number, change):
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == number])
    repo = "feder-positronics/intelflo"
    target = TARGETS[number]
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
    assert bool(found) is (change == "none")


@pytest.mark.parametrize("number", TARGETS)
def test_replacement_budget_counts_normally_and_originals_are_unchanged(
    number, tmp_path, monkeypatch
):
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == number])
    snapshot = copy.deepcopy(rows)
    monkeypatch.setattr(cli, "_is_ancestor", lambda *a: True)
    target = TARGETS[number]

    def markers(history):
        return cli._lineage_markers(
            tmp_path, history, target, PUBLISHER, repo="feder-positronics/intelflo", number=number
        )

    assert cli._decide_kind(markers(rows), target) == ("primary", None)
    replacement = {
        "id": 999,
        "commit_id": target,
        "user": {"login": PUBLISHER[0]},
        "state": "COMMENTED",
        "body": cli.review_marker(target, "primary") + "\n**approve**",
    }
    with pytest.raises(cli.CliError, match="already has a primary"):
        cli._decide_kind(markers([*rows, replacement]), target)
    delta = {
        **replacement,
        "id": 1000,
        "body": cli.review_marker(target, "delta") + "\n**approve**",
    }
    with pytest.raises(cli.CliError, match="budget exhausted"):
        cli._decide_kind(markers([*rows, replacement, delta]), target)
    assert rows == snapshot


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


def inspected_replacement(number):
    row = copy.deepcopy(next(r for r in RECEIPTS if r["number"] == number))
    row.update(id=999, commit_id=TARGETS[number])
    native = _native_codex_result(
        TARGETS[number], repo=row["repo"], number=number, publisher=PUBLISHER[0]
    )
    return publish_replacement(row, json.loads(native.raw))


@pytest.mark.parametrize("number", TARGETS)
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "model",
        "effort",
        "session",
        "no_tool",
        "failed_tool",
        "wrong_head",
        "generic_tool",
        "quoted_git",
        "no_link",
    ],
)
def test_recovered_eligibility_requires_actual_attributable_inspection(number, change):
    row = inspected_replacement(number)
    raw = envelope(row)
    if change in ("model", "effort"):
        raw["reported_settings"][change] = None
    if change == "session":
        raw["session_id"] = None
    tool = next(
        e["item"]
        for e in raw["events"]
        if e.get("type") == "item.completed"
        and e.get("item", {}).get("type") == "command_execution"
    )
    if change == "no_tool":
        raw["events"] = [
            e for e in raw["events"] if e.get("item", {}).get("type") != "command_execution"
        ]
    if change == "failed_tool":
        tool["exit_code"] = 1
    if change == "wrong_head":
        tool["aggregated_output"] = "f" * 40 + "\ndiff --git a/a b/a"
    if change == "quoted_git":
        tool["command"] = "echo 'git rev-parse HEAD && git diff'"
    if change == "generic_tool":
        tool["aggregated_output"] = TARGETS[number] + "\nhello"
    if change == "session":
        with pytest.raises(runners.RunnerBadOutput):
            publish_replacement(copy.deepcopy(row), raw)
        import base64

        line = row["body"].splitlines()[1]
        projection = runners.decode_codex_evidence(line)
        projection["calls"][0]["session"] = None
        token = (
            base64.urlsafe_b64encode(
                json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )
        row["body"] = row["body"].replace(line, runners.CODEX_HEADER + token + " -->")
    else:
        publish_replacement(row, raw)
    if change == "no_link":
        row["body"] = row["body"].replace("loopzero:adjudication", "removed:adjudication")
    reviews = [r for r in RECEIPTS if r["number"] == number] + [row]
    found = eligible_recovery(reviews, number, TARGETS[number])
    assert (found is not None) == (change == "none")


@pytest.mark.parametrize("mutation", ["publisher", "body", "id", "head", "target"])
def test_mismatched_historical_identity_holds_budget_and_eligibility(
    tmp_path, monkeypatch, mutation
):
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == 9284])
    target = TARGETS[9284]
    if mutation == "publisher":
        rows[0]["user"]["login"] = "stranger"
    if mutation == "body":
        rows[0]["body"] += " "
    if mutation == "id":
        rows[0]["id"] += 1
    if mutation == "head":
        rows[0]["commit_id"] = "b" * 40
    if mutation == "target":
        target = "c" * 40
    monkeypatch.setattr(cli, "_is_ancestor", lambda *a: True)
    with pytest.raises(cli.CliError, match="unverified; HOLD"):
        cli._lineage_markers(
            tmp_path, rows, target, PUBLISHER, repo="feder-positronics/intelflo", number=9284
        )
    assert eligible_recovery([*rows, inspected_replacement(9284)], 9284, target) is None


@pytest.mark.parametrize("number", [9179, 9173, 9174, 9175])
def test_adjudicated_primary_recovers_without_changing_other_receipts(tmp_path, monkeypatch, number):
    snapshot = copy.deepcopy(RECEIPTS)
    monkeypatch.setattr(cli, "_is_ancestor", lambda *a: True)
    assert eligibility.recovery_baseline(number, RECEIPTS)
    for other in (9284, 9275, 9220):
        assert eligibility.recovery_baseline(other, RECEIPTS)
    rows = [r for r in RECEIPTS if r["number"] == number]
    def markers(history):
        return cli._lineage_markers(
            tmp_path, history, TARGETS[number], PUBLISHER,
            repo="feder-positronics/intelflo", number=number,
        )
    assert cli._decide_kind(markers(rows), TARGETS[number]) == ("primary", None)
    assert eligible_recovery([*rows, inspected_replacement(number)], number, TARGETS[number])
    for omitted in rows:
        incomplete = [r for r in rows if r["id"] != omitted["id"]]
        with pytest.raises(cli.CliError, match="unverified; HOLD"):
            markers(incomplete)
    assert RECEIPTS == snapshot


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "unknown",
        "fatal_latest",
        "open_finding",
        "publication_recovery",
        "repost",
        "repost_confirmation",
    ],
)
@pytest.mark.parametrize("number", [9284, 9275])
def test_local_hosted_agree_and_historical_findings_stay_blocking(
    wt, gh, case, monkeypatch, number
):
    from loopzero import github
    from loopzero.types import CheckReport, Config
    from tests.test_github import marker, pr_json, rest_pr_json, thread, threads_json

    target = TARGETS[number]
    repository = "feder-positronics/intelflo"
    rows = copy.deepcopy([r for r in RECEIPTS if r["number"] == number])
    replacement = inspected_replacement(number)
    if case in {"repost", "repost_confirmation"}:
        native = result(replacement)
        suffix = eligibility.recovery_suffix(repository, number, target, RECEIPTS)
        marker, _, annotation = cli._repost_prefix(native).partition("\n")
        replacement["body"] = github._review_body(native, [], marker + suffix + "\n" + annotation)
        assert replacement["body"].splitlines()[2] == suffix.lstrip("\n")
    if case == "unknown":
        raw = envelope(replacement)
        raw["reported_settings"]["model"] = None
        publish_replacement(replacement, raw)
    reviews = [*rows, replacement]
    if case == "fatal_latest":
        reviews.append(rows[-1])
    gh.respond(
        f"api repos/{repository}/pulls/{number}",
        rest_pr_json(pr_json(number=number, headRefOid=target, mergeStateStatus="CLEAN")),
    )
    nodes = (
        [thread(marker("important", target) + "\nStill unresolved")]
        if case == "open_finding"
        else []
    )
    gh.respond("api graphql", threads_json(*nodes))
    cfg = Config(repository, "main", (), (), "squash", ("codex",), review_publishers=PUBLISHER)
    if case in {"publication_recovery", "repost_confirmation"}:
        gh.respond(f"api repos/{repository}/pulls/{number}/files", [])
        gh.respond(
            f"api repos/{repository}/pulls/{number}/reviews/999/comments?per_page=100&page=1", []
        )
        native = result(replacement)
        monkeypatch.setattr(cli.worktree, "head", lambda path: target)
        gh.respond(f"api repos/{repository}/pulls/{number}/reviews?per_page=100&page=1", reviews)
        assert cli._confirm_review(
            wt, cfg, github.pr_view(repository, number), native, PUBLISHER[0], reviews
        )
    gh.respond(f"api repos/{repository}/pulls/{number}/reviews?per_page=100&page=1", reviews)
    cli._save_report(wt, CheckReport(target, False, ()))
    local = cli._readiness(wt, cfg, github.pr_view(repository, number), target, reviews)
    hosted = eligibility.evaluate(repository, number, target, "main", PUBLISHER)
    assert (
        local.ready
        is hosted.ready
        is (case in {"valid", "publication_recovery", "repost", "repost_confirmation"})
    )
    assert local.reasons == hosted.reasons
    if case == "open_finding":
        assert any("open important" in reason for reason in local.reasons)
    original_ids = (5667628969, 5667664997) if number == 9284 else (5666873014, 5667433401)
    assert [r for r in reviews if r["id"] in original_ids][:2] == rows


@pytest.mark.parametrize("number", [9284, 9275])
def test_saved_target_repost_publishes_canonical_block(wt, gh, monkeypatch, number):
    from argparse import Namespace

    from loopzero.types import Config
    from tests.test_github import pr_json, rest_pr_json

    replacement = inspected_replacement(number)
    native = result(replacement)
    repository, target = replacement["repo"], replacement["commit_id"]
    original = [row for row in RECEIPTS if row["number"] == number]
    cfg = Config(
        repository, "main", (), (), "squash", ("codex", "claude"), review_publishers=PUBLISHER
    )
    pr = github._pr_from_rest(rest_pr_json(pr_json(number=number, headRefOid=target)))
    cli._save_review(wt, native)
    monkeypatch.setattr(cli, "_context", lambda *a, **k: (wt, cfg))
    monkeypatch.setattr(cli, "_require_pr", lambda *a, **k: pr)
    monkeypatch.setattr(cli.worktree, "head", lambda path: target)
    monkeypatch.setattr(cli.runners, "author_families", lambda *a: {"claude"})
    monkeypatch.setattr(cli.github, "login", lambda **k: PUBLISHER[0])
    monkeypatch.setattr(cli.github, "app_token", lambda: "")
    monkeypatch.setattr(cli.github, "update_body", lambda *a: None)
    monkeypatch.setattr(
        cli.runners, "review_with", lambda *a, **k: pytest.fail("repost must not rerun a model")
    )
    gh.respond(f"api repos/{repository}/pulls/{number}/reviews?per_page=100&page=1", original)
    gh.respond(f"api repos/{repository}/pulls/{number}/files", [])
    gh.respond(f"api repos/{repository}/pulls/{number}/reviews", {"id": 999})
    args = Namespace(config=None, repost=True, model=None, effort=None)
    assert cli.cmd_review(args) == 0
    posted = json.loads(
        next(
            c["--input"]
            for c in gh.calls
            if c["argv"][1] == f"repos/{repository}/pulls/{number}/reviews"
        )
    )
    lines = posted["body"].splitlines()
    assert lines[0] == cli.review_marker(target, "primary", "repost")
    assert lines[1].startswith(runners.CODEX_HEADER)
    assert lines[2] == eligibility.recovery_suffix(repository, number, target, original).lstrip(
        "\n"
    )
    assert lines[3].startswith("Reposted from runner session")
    replacement["body"] = posted["body"]
    assert eligible_recovery(original + [replacement], number, target)
