"""Public review recovery against a GitHub transport that loses its POST reply."""

import copy
import json

import pytest

from loopzero import _proc, cli, github
from tests import test_cli as support
from tests.conftest import git
from tests.test_cli import (
    APPROVE,
    CHANGES,
    LOGIN,
    REPO,
    arm_pr,
    expect_cli,
    head_of,
    rev,
    reviews_key,
    run,
)

gh, git_seed, repo, wt = support.gh, support.git_seed, support.repo, support.wt
replay_gh = support.replay_gh
REVIEW_COMMENTS = f"api repos/{REPO}/pulls/7/reviews/17/comments?per_page=100&page=1"


def published_review(head, body, *, state="COMMENTED"):
    return {"id": 17, "commit_id": head, "body": body,
            "state": state, "user": {"login": LOGIN}}


def arm_published_review(gh, head, body, *, state="COMMENTED"):
    gh.respond(reviews_key(), [published_review(head, body, state=state)])


def lost_reply(wt, gh, fake_bin, monkeypatch, *, kind, verdict, interrupted=False,
               mutate=None, timeout=False, findings=None):
    history = []
    if kind == "delta":
        history = [rev(head_of(wt), "primary")]
        (wt / "feature.py").write_text("print('fixed')\n")
        git(wt, "commit", "-qam", "repair")
    payload = APPROVE if verdict == 0 else CHANGES
    if findings is not None:
        payload = {**payload, "findings": findings}
    head = support.prepare_review(wt, gh, fake_bin, history=history, post_id=17, payload=payload)
    reviewer = fake_bin / "claude"
    reviewer.write_text(reviewer.read_text().replace(
        "#!/bin/sh\n", f'#!/bin/sh\nprintf "run\\n" >> "{fake_bin}/claude.runs"\n'))
    original = github._api

    def transport(endpoint, payload, **kwargs):
        response = original(endpoint, payload, **kwargs)
        if endpoint.endswith("/reviews"):
            review = published_review(head, payload["body"])
            comments = [{**c, "commit_id": head, "user": {"login": LOGIN},
                         "pull_request_review_id": 17} for c in payload["comments"]]
            rows = [*history, review]
            if mutate:
                mutate(rows, comments)
            gh.respond(reviews_key(), rows)
            gh.respond(REVIEW_COMMENTS,
                       comments)
            if interrupted:
                raise KeyboardInterrupt
            if timeout:
                raise _proc.ProcTimeout("gh POST timed out")
            raise github.GhError(("gh", "api", endpoint), "client timed out after acceptance")
        return response

    monkeypatch.setattr(github, "_api", transport)
    return head


@pytest.fixture
def interrupted_receipt(wt, gh, fake_bin, monkeypatch, capsys):
    original = github._api
    head = lost_reply(wt, gh, fake_bin, monkeypatch, kind="primary", verdict=0, interrupted=True)
    assert run(capsys, "review")[0] == 130
    path = cli._review_file(wt, head, "primary")
    return head, path, json.loads(path.read_text()), original


def assert_single_publication(gh, fake_bin):
    assert (fake_bin / "claude.runs").read_text().splitlines() == ["run"]
    assert len([c for c in gh.calls if "--input" in c]) == 1


@pytest.mark.parametrize("override", [None, "pr", "head", "raw", "bytes", "effort"])
def test_plain_review_surfaces_receipt_after_interruption_before_post(
    wt, gh, fake_bin, monkeypatch, capsys, override,
):
    head = support.prepare_review(wt, gh, fake_bin)
    reviewer = fake_bin / "claude"
    reviewer.write_text(reviewer.read_text().replace(
        "#!/bin/sh\n", f'#!/bin/sh\nprintf "run\\n" >> "{fake_bin}/claude.runs"\n'))
    original = github.post_review

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(github, "post_review", interrupt)
    assert run(capsys, "review")[0] == 130
    monkeypatch.setattr(github, "post_review", original)

    path = cli._review_file(wt, head, "primary")
    saved = path.read_bytes()
    if override in {"pr", "head", "raw"}:
        payload = json.loads(saved)
        if override == "pr":
            payload["publication"]["number"] = 999
        else:
            payload[override] = "invalid"
        path.write_text(json.dumps(payload))
    if override == "bytes":
        path.write_bytes(b"\xff")
    args = ["--effort", "high"] if override == "effort" else []
    code, _out, err = run(capsys, "review", *args)

    if override is None:
        assert code == 1 and "loopzero review --repost" in err
        assert path.read_bytes() == saved
        assert not any("--input" in c for c in gh.calls)
        assert run(capsys, "review", "--repost")[0] == 0
        assert (fake_bin / "claude.runs").read_text().splitlines() == ["run"]
        assert sum(
            c["argv"][1] == support.post_key().split()[1] and "--input" in c for c in gh.calls
        ) == 1
    else:
        assert code == 0, err
        assert (fake_bin / "claude.runs").read_text().splitlines() == ["run", "run"]


@pytest.mark.parametrize("kind", ["primary", "delta"])
@pytest.mark.parametrize("verdict", [0, 5])
@pytest.mark.parametrize("interrupted", [False, True, "timeout"])
def test_accepted_publication_recovers_saved_verdict(
    wt, gh, fake_bin, monkeypatch, capsys, kind, verdict, interrupted, replay_gh,
):
    lost_reply(wt, gh, fake_bin, monkeypatch, kind=kind, verdict=verdict,
               interrupted=interrupted is True, timeout=interrupted == "timeout")
    if interrupted is True:
        expect_cli(capsys, 130, "review")
        (fake_bin / "claude.stdin").unlink()
    out, _ = expect_cli(capsys, verdict, "review", *(("--repost",) if interrupted is True else ()))
    if interrupted is True:
        assert not (fake_bin / "claude.stdin").exists(), "recovery must not launch a reviewer"
    assert out.splitlines()[-1].startswith(f"{kind} review by claude")
    assert interrupted is True or out.startswith("reviewer paths pinned to origin/main@")
    assert_single_publication(gh, fake_bin)
    if verdict == 5:
        files = ["api", f"repos/{REPO}/pulls/7/files"]
        assert sum(call["argv"][:2] == files for call in gh.calls) == 2


@pytest.mark.parametrize("change", ["body", "comment", "location", "publisher", "pending",
                                     "dismissed", "newer", "duplicates", "bad_page", "auth", "quota",
                                     "head", "comment_page", "kind", "wrong_head"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_recovery_requires_exact_live_publication(
    wt, gh, fake_bin, monkeypatch, capsys, change, interrupted, replay_gh,
):
    def mutate(rows, comments):
        if change == "body":
            rows[-1]["body"] += "\nchanged"
        elif change == "comment":
            comments[0]["body"] += "changed"
        elif change == "location":
            comments[0]["line"] = 99
        elif change == "publisher":
            rows[-1]["user"]["login"] = "another-publisher"
        elif change in {"pending", "dismissed"}:
            rows[-1]["state"] = change.upper()
        elif change in {"newer", "duplicates"}:
            rows.append(copy.deepcopy(rows[-1]))
            rows[-1]["id"] = 18
            if change == "newer":
                rows[-1]["state"] = "PENDING"
            gh.respond(f"api repos/{REPO}/pulls/7/reviews/18/comments?per_page=100&page=1",
                       [{**c, "pull_request_review_id": 18} for c in comments])
        elif change == "head":
            arm_pr(gh, "b" * 40)
        elif change == "kind":
            rows[-1]["body"] = rows[-1]["body"].replace("kind=delta", "kind=primary")
        elif change == "wrong_head":
            rows[-1]["commit_id"] = "b" * 40
        elif change == "comment_page":
            comments[:] = comments * 100
            gh.respond(f"api repos/{REPO}/pulls/7/reviews/17/comments?per_page=100&page=2", None)
        elif change in {"bad_page", "auth", "quota"}:
            rows[:] = [copy.deepcopy(rows[-1]) for _ in range(100)]
            gh.respond(reviews_key(2), None)
            if change != "bad_page":
                gh.fail(reviews_key(2), "not logged in" if change == "auth" else "HTTP 429")
    lost_reply(wt, gh, fake_bin, monkeypatch, kind="delta", verdict=5, mutate=mutate,
               interrupted=interrupted)
    if interrupted:
        expect_cli(capsys, 130, "review")
    out, _ = expect_cli(capsys, 5 if change == "duplicates" else 1, "review", *(("--repost",) if interrupted else ()))
    if change != "duplicates":
        assert not out if interrupted else (
            out.startswith("reviewer paths pinned to origin/main@") and len(out.splitlines()) == 1
        )
    assert len([c for c in gh.calls if "--input" in c]) == 1


@pytest.mark.parametrize("change", ["login", "raw", "repo", "publisher", "number"])
def test_interrupted_publication_uses_valid_saved_runner_and_request_identity(
    wt, gh, fake_bin, monkeypatch, capsys, change,
):
    head = lost_reply(wt, gh, fake_bin, monkeypatch, kind="delta", verdict=5, interrupted=True)
    assert run(capsys, "review")[0] == 130
    path = wt / ".loopzero" / f"review-{head[:12]}-delta.json"
    saved = json.loads(path.read_text())
    if change == "login":
        gh.respond("api graphql viewer", {"data": {"viewer": {"login": "other-login"}}})
    elif change == "raw":
        saved["raw"] = "{}"
    else:
        saved["publication"][change] = "wrong"
    path.write_text(json.dumps(saved))
    expect_cli(capsys, 5 if change == "login" else 1, "review", "--repost")
    assert len([c for c in gh.calls if "--input" in c]) == 1


@pytest.mark.parametrize("change", ["revoked", "deadline", "read_timeout"])
def test_recovery_cannot_outlive_deadline_or_live_review_evidence(
    wt, gh, fake_bin, monkeypatch, capsys, change,
):
    lost_reply(wt, gh, fake_bin, monkeypatch, kind="delta", verdict=5)
    original, monotonic = github.api_get, github.time.monotonic
    def read(endpoint):
        data = original(endpoint)
        if "/comments?" in endpoint:
            if change == "revoked":
                rows = original(reviews_key().removeprefix("api "))
                rows[-1]["state"] = "DISMISSED"
                gh.respond(reviews_key(), rows)
            elif change == "deadline":
                monkeypatch.setattr(github.time, "monotonic", lambda: monotonic() + 31)
            else:
                raise _proc.ProcTimeout("read timed out")
        return data
    monkeypatch.setattr(github, "api_get", read)
    out, _ = expect_cli(capsys, 1, "review", stderr_contains=("read-back blocked",))
    assert out.startswith("reviewer paths pinned to origin/main@") and len(out.splitlines()) == 1
    assert len([c for c in gh.calls if "--input" in c]) == 1


def test_repost_recovers_review_with_redacted_findings(wt, gh, fake_bin, monkeypatch, capsys):
    title = "Rotate sk-ant-FAKE0123"
    findings = [
        {"severity": "important", "path": "feature.py", "line": 1,
         "title": title, "body": "Remove sk-proj-Fake_only-0123 from settings."},
        {"severity": "suggestion", "path": None, "line": None,
         "title": "Replace ghp_FAKE0123456789", "body": "See sk-FakeToken0123 in the log."},
    ]
    head = lost_reply(wt, gh, fake_bin, monkeypatch, kind="primary", verdict=5,
                      interrupted=True, findings=findings)
    assert run(capsys, "review")[0] == 130
    payload = support.posted_review(gh)
    assert "**suggestion**: Replace [REDACTED] — See [REDACTED] in the log." in payload["body"]
    comment, = payload["comments"]
    assert (comment["path"], comment["line"], comment["side"]) == ("feature.py", 1, "RIGHT")
    stable_id = github.finding_id(head, "feature.py", 1, title)
    assert comment["body"].splitlines()[0].endswith(f"severity=important head={head} id={stable_id} -->")
    assert "**important: Rotate [REDACTED]**\n\nRemove [REDACTED] from settings." in comment["body"]
    assert run(capsys, "review", "--repost")[0] == 5
    assert_single_publication(gh, fake_bin)


def test_interrupted_explicit_repost_recovers_repost_body(wt, gh, fake_bin, monkeypatch, capsys):
    head = lost_reply(wt, gh, fake_bin, monkeypatch, kind="primary", verdict=5, interrupted=True)
    original = github._api
    def rejected(endpoint, payload, **kwargs):
        if endpoint.endswith("/reviews"):
            raise github.GhError(("gh", "api", endpoint), "explicit server rejection")
        return original(endpoint, payload, **kwargs)
    monkeypatch.setattr(github, "_api", rejected)
    assert run(capsys, "review")[0] == 1
    monkeypatch.setattr(github, "_api", original)
    assert run(capsys, "review", "--repost")[0] == 130
    (fake_bin / "claude.stdin").unlink()
    code, out, err = run(capsys, "review", "--repost")
    assert code == 5 and out.startswith("primary review by claude"), err
    assert not (fake_bin / "claude.stdin").exists()
    assert len([c for c in gh.calls if "--input" in c]) == 1
    assert support.marker(head, "primary", "repost") in support.posted_review(gh)["body"]


@pytest.mark.parametrize("failure,budget", [("timeout", 0.5), ("transient", 0.5), ("transient", 10), ("write", 0.5)])
def test_recovery_transport_retries_only_within_deadline(monkeypatch, failure, budget):
    timeouts, sleeps = [], []
    def request(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if failure in {"timeout", "write"}:
            raise _proc.ProcTimeout("timeout")
        return _proc.Completed(1, "", "HTTP 503", 0)
    monkeypatch.setattr(github, "run", request)
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(github.time, "monotonic", lambda: 10.0 + sum(sleeps) + int(failure == "write" and bool(timeouts)))
    error = github.GhError if failure == "transient" else _proc.ProcTimeout
    with pytest.raises(error), github.bounded_reads(10 + budget, retry=True):
        github.mark_ready("acme/widgets", 7) if failure == "write" else github.api_get("repos/acme/widgets/pulls/7/reviews")
    assert (timeouts, sleeps) == (([10, 5], [5]) if budget == 10 else ([120 if failure == "write" else 0.5], []))


@pytest.mark.parametrize("source", ["model", "repost"])
@pytest.mark.parametrize("metadata,shape", [
    ("legacy", "single"), ("legacy", "chunked"), ("legacy", "truncated"),
    ("null", "single"), ("empty", "single"), ("malformed", "single"),
])
def test_historical_publication_recovery_is_exact_and_legacy_only(
    replay_gh, interrupted_receipt, gh, fake_bin, capsys, metadata, source, shape,
):
    head, path, saved, _ = interrupted_receipt
    envelope = json.loads(saved["raw"])
    if shape == "chunked":
        saved["chunk_count"] = 2
        saved["raw"] = json.dumps([envelope, envelope], separators=(",", ":"))
        saved["provenance"]["session_ids"] *= 2
        saved["provenance"]["usage"] = [envelope["usage"], envelope["usage"]]
        for key in ("total_cost_usd", "duration_api_ms"):
            saved["provenance"][key] *= 2
    elif shape == "truncated":
        saved["raw"] = json.dumps({**envelope, "padding": "é" * 20_000}, ensure_ascii=False)
    if metadata == "legacy":
        saved["provenance"].pop("requested_settings", None)
    else:
        saved["provenance"]["requested_settings"] = {"null": None, "empty": [], "malformed": "bad"}[metadata]
    path.write_text(json.dumps(saved))
    prefix = support.marker(head, "primary", source)
    if source == "repost":
        prefix += ("\nReposted from runner sessions: `claude-session-123`, `claude-session-123`"
                   if shape == "chunked" else "\nReposted from runner session: `claude-session-123`")
    chunks = "\n\nReviewed in 2 chunks with the same model family." if shape == "chunked" else ""
    transcript = saved["raw"]
    if shape == "truncated":
        transcript = ("[Transcript truncated; showing the last at most 30,000 UTF-8 bytes. "
                      "Full output remains in the saved local review artifact.]\n"
                      + transcript.encode()[-30_000:].decode(errors="ignore"))
    effort = f" (effort {saved['effort']})" if saved["effort"] else ""
    body = (f"{prefix}\nloopzero primary review by claude on `{head}`, "
            f"model {saved['model'] or 'unknown'}{effort}, {saved['duration_s']:.1f}s: "
            f"**approve** (0 critical, 0 important, 0 suggestion){chunks}\n\n"
            f"<details><summary>Raw reviewer output</summary>\n\n```\n{transcript}\n```\n</details>")
    arm_published_review(gh, head, body)
    expect_cli(capsys, 0 if metadata == "legacy" else 1, "review", "--repost")
    assert_single_publication(gh, fake_bin)
    if metadata == "legacy":
        mixed = body.partition("\n\n<details>")[0] + (
            "\n\nNative Claude output is retained in the restricted local review receipt."
        )
        for altered_body in (mixed, body + "changed"):
            arm_published_review(gh, head, altered_body)
            expect_cli(capsys, 1, "review", "--repost")
        arm_published_review(gh, head, body)
        gh.respond(REVIEW_COMMENTS, [
            {"path": "feature.py", "line": 1, "side": "RIGHT", "body": "changed finding",
             "commit_id": head, "pull_request_review_id": 17, "user": {"login": LOGIN}},
        ])
        assert run(capsys, "review", "--repost")[0] == 1


@pytest.mark.parametrize("optional", ["junk", [], [{"model": "request"}] * 5])
def test_optional_settings_cannot_block_explicit_repost(
    interrupted_receipt, gh, fake_bin, monkeypatch, capsys, optional,
):
    _, path, saved, original = interrupted_receipt
    saved["provenance"]["requested_settings"] = optional
    saved["provenance"]["reported_settings"] = "forged"
    path.write_text(json.dumps(saved))
    gh.respond(reviews_key(), [])
    monkeypatch.setattr(github, "_api", original)
    expect_cli(capsys, 0, "review", "--repost")
    body = support.posted_review(gh)["body"]
    assert "CLI-reported model unknown (effort unknown)" in body and "forged" not in body
    assert (fake_bin / "claude.runs").read_text().splitlines() == ["run"]


@pytest.mark.parametrize("published", [True, False])
def test_repost_confirms_published_legacy_fallback_note_but_never_posts_one(
    wt, gh, fake_bin, fake_tool, monkeypatch, capsys, published,
):
    legacy = "independent reviewers failed: codex: /home/u/secret: boom"
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    fake_tool("codex", "if [ \"$1\" = --version ]; then echo 'codex-cli 0.161.0'; exit 0; fi\n"
              "echo 'Please run codex login' >&2\nexit 1\n")

    def mutate(rows, comments):  # an older version published the raw runner line
        if published:
            rows[-1]["body"] = rows[-1]["body"].replace(
                "independent reviewers failed: codex: not authenticated", legacy)
        else:
            rows.pop()

    head = lost_reply(wt, gh, fake_bin, monkeypatch, kind="primary", verdict=0,
                      interrupted=True, mutate=mutate)
    assert run(capsys, "review")[0] == 130
    receipt = cli._review_file(wt, head, "primary")
    data = json.loads(receipt.read_text())
    assert data["fallback"] == "independent reviewers failed: codex: not authenticated"
    receipt.write_text(json.dumps({**data, "fallback": legacy}))
    errors = () if published else ("not in the published form",)
    expect_cli(capsys, 0 if published else 1, "review", "--repost", stderr_contains=errors)
    assert len([c for c in gh.calls if "--input" in c]) == 1


@pytest.mark.parametrize("publication", ["pre-configured", "transcript", "summary"])
def test_repost_confirms_historical_claude_publication(
    interrupted_receipt, gh, fake_bin, capsys, publication,
):
    head, path, saved, _ = interrupted_receipt
    request = saved["provenance"]["requested_settings"][0]
    settings = ("CLI claims" if publication == "pre-configured"
                else "launch configuration and CLI claims")
    body = (
        f'{support.marker(head, "primary", "model")}\n'
        f"loopzero primary review by claude on `{head}`, {saved['duration_s']:.1f}s: "
        "**approve** (0 critical, 0 important, 0 suggestion)\n\n"
        f"Settings per native call ({settings}, not backend attestation):\n"
        f"- Call 1: requested model {request['model'] or 'unknown'} "
        f"(effort {request['effort'] or 'unknown'}); "
        f"CLI-reported model {saved['model'] or 'unknown'} (effort {saved['effort'] or 'unknown'})"
    )
    if publication != "pre-configured":
        body += "; configured model unknown (effort unknown)"
    if publication == "summary":
        body += "\n\nNative Claude output is retained in the restricted local review receipt."
    else:
        body += f"\n\n<details><summary>Raw reviewer output</summary>\n\n```\n{saved['raw']}\n```\n</details>"
    arm_published_review(gh, head, body)
    if publication == "pre-configured":
        assert "configured_settings" in saved["provenance"]
        assert run(capsys, "review", "--repost")[0] == 1
        saved["provenance"].pop("configured_settings")
        path.write_text(json.dumps(saved))
    out, err = expect_cli(capsys, 0, "review", "--repost")
    assert saved["raw"] not in out + err
    if publication == "pre-configured":
        arm_published_review(gh, head, body + "changed")
        assert run(capsys, "review", "--repost")[0] == 1
    assert_single_publication(gh, fake_bin)


@pytest.mark.parametrize("altered", [False, True])
def test_repost_confirms_pre_configured_codex_publication(wt, gh, capsys, monkeypatch, altered):
    head = head_of(wt)
    arm_pr(gh, head)
    result = support._native_codex_result(head, repo=REPO, publisher=LOGIN, cwd=str(wt))
    cli._save_review(wt, result, {"repo": REPO, "number": 7, "publisher": LOGIN})
    evidence = cli.runners.codex_evidence(result)
    call = evidence["calls"][0]
    assert "configured" not in call
    body = (
        f'{support.marker(head, "primary", "model")}\n'
        f"{cli.runners.encode_codex_evidence(evidence)}\n"
        f"loopzero primary review by codex on `{head}`, unknown: "
        "**approve** (0 critical, 0 important, 0 suggestion)\n\n"
        "Settings per native call (CLI claims, not backend attestation):\n"
        f"- Call 1: requested model {call['requested']['model']} "
        f"(effort {call['requested']['effort']}); "
        f"CLI-reported model {call['reported']['model']} (effort {call['reported']['effort']})"
        "\n\n<details><summary>Raw reviewer output</summary>\n\n```\n"
        "Native Codex transcript retained in the restricted local review receipt; "
        "public evidence is publisher-derived, not backend attestation. "
        f"Exact raw SHA256: {evidence['raw_sha256']}\n```\n</details>"
    )
    if altered:
        body += "changed"
    arm_published_review(gh, head, body, state="APPROVED")
    gh.respond(REVIEW_COMMENTS, [])

    def unexpected_launch(*args, **kwargs):
        pytest.fail("repost launched a reviewer")

    monkeypatch.setattr(cli.runners, "review_with", unexpected_launch)
    _, err = expect_cli(capsys, int(altered), "review", "--repost")
    if altered:
        assert "conflicting or ambiguous review publication" in err
    assert all("--input" not in call for call in gh.calls)
