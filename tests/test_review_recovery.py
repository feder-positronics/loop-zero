"""Public review recovery against a GitHub transport that loses its POST reply."""

import copy
import json

import pytest

from loopzero import _proc, github
from tests import test_cli as support
from tests.conftest import git
from tests.test_cli import (
    APPROVE,
    CHANGES,
    LOGIN,
    REPO,
    arm_pr,
    head_of,
    rev,
    reviews_key,
    run,
)

gh, repo, wt = support.gh, support.repo, support.wt


def lost_reply(wt, gh, fake_bin, monkeypatch, *, kind, verdict, interrupted=False,
               mutate=None, timeout=False):
    history = []
    if kind == "delta":
        history = [rev(head_of(wt), "primary")]
        (wt / "feature.py").write_text("print('fixed')\n")
        git(wt, "commit", "-qam", "repair")
    head = support.prepare_review(wt, gh, fake_bin, history=history, post_id=17,
                                  payload=APPROVE if verdict == 0 else CHANGES)
    reviewer = fake_bin / "claude"
    reviewer.write_text(reviewer.read_text().replace(
        "#!/bin/sh\n", f'#!/bin/sh\nprintf "run\\n" >> "{fake_bin}/claude.runs"\n'))
    original = github._api

    def transport(endpoint, payload, **kwargs):
        response = original(endpoint, payload, **kwargs)
        if endpoint.endswith("/reviews"):
            review = {"id": 17, "commit_id": head, "body": payload["body"],
                      "state": "COMMENTED", "user": {"login": LOGIN}}
            comments = [{**c, "commit_id": head, "user": {"login": LOGIN},
                         "pull_request_review_id": 17} for c in payload["comments"]]
            rows = history + [review]
            if mutate:
                mutate(rows, comments)
            gh.respond(reviews_key(), rows)
            gh.respond(f"api repos/{REPO}/pulls/7/reviews/17/comments?per_page=100&page=1",
                       comments)
            if interrupted:
                raise KeyboardInterrupt
            if timeout:
                raise _proc.ProcTimeout("gh POST timed out")
            raise github.GhError(("gh", "api", endpoint), "client timed out after acceptance")
        return response

    monkeypatch.setattr(github, "_api", transport)
    return head


@pytest.mark.parametrize("kind", ["primary", "delta"])
@pytest.mark.parametrize("verdict", [0, 5])
@pytest.mark.parametrize("interrupted", [False, True, "timeout"])
def test_accepted_publication_recovers_saved_verdict(
    wt, gh, fake_bin, monkeypatch, capsys, kind, verdict, interrupted,
):
    lost_reply(wt, gh, fake_bin, monkeypatch, kind=kind, verdict=verdict,
               interrupted=interrupted is True, timeout=interrupted == "timeout")
    if interrupted is True:
        assert run(capsys, "review")[0] == 130
        (fake_bin / "claude.stdin").unlink()
        code, out, err = run(capsys, "review", "--repost")
        assert not (fake_bin / "claude.stdin").exists(), "recovery must not launch a reviewer"
    else:
        code, out, err = run(capsys, "review")
    assert code == verdict, err
    assert out.splitlines()[-1].startswith(f"{kind} review by claude")
    assert interrupted is True or out.startswith("reviewer paths pinned to origin/main@")
    assert (fake_bin / "claude.runs").read_text().splitlines() == ["run"]
    writes = [c for c in gh.calls if "--input" in c]
    assert len(writes) == 1, "confirmed recovery must not write again, including the PR body"


@pytest.mark.parametrize("change", ["body", "comment", "location", "publisher", "pending",
                                     "dismissed", "newer", "duplicates", "bad_page", "auth", "quota",
                                     "head", "comment_page", "kind", "wrong_head"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_recovery_requires_exact_live_publication(
    wt, gh, fake_bin, monkeypatch, capsys, change, interrupted,
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
    code, out, err = run(capsys, "review")
    if interrupted:
        assert code == 130
        code, out, err = run(capsys, "review", "--repost")
    assert code == (5 if change == "duplicates" else 1), err
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
    code, _out, err = run(capsys, "review", "--repost")
    assert code == (5 if change == "login" else 1), err
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
    code, out, err = run(capsys, "review")
    assert code == 1 and "read-back blocked" in err
    assert out.startswith("reviewer paths pinned to origin/main@") and len(out.splitlines()) == 1
    assert len([c for c in gh.calls if "--input" in c]) == 1


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


@pytest.mark.parametrize("failure", ["timeout", "transient"])
def test_recovery_transport_caps_request_time_and_does_not_retry(monkeypatch, failure):
    timeouts = []
    def request(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if failure == "timeout":
            raise _proc.ProcTimeout("timeout")
        return _proc.Completed(1, "", "HTTP 503", 0)
    monkeypatch.setattr(github, "run", request)
    monkeypatch.setattr(github, "app_token", lambda: "")
    with github.bounded_reads(0.5), pytest.raises((github.GhError, _proc.ProcTimeout)):
        github.api_get("repos/acme/widgets/pulls/7/reviews")
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.5
