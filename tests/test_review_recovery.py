"""Public review recovery against a GitHub transport that loses its POST reply."""

import copy
import json
from unittest.mock import Mock

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
REVIEW_COMMENTS = f"api repos/{REPO}/pulls/7/reviews/17/comments --method GET"


def published_review(head, body, *, state="COMMENTED"):
    return {"id": 17, "commit_id": head, "body": body,
            "state": state, "user": {"login": LOGIN}}


def record_claude_runs(fake_bin):
    reviewer = fake_bin / "claude"
    shebang = "#!/bin/sh\n"
    reviewer.write_text(reviewer.read_text().replace(shebang, shebang + f'echo run >> "{fake_bin}/claude.runs"\n'))


def lost_reply(wt, gh, fake_bin, monkeypatch, *, kind="primary", verdict=0, failure="lost-reply", mutate=None, findings=None):
    history = [rev(head_of(wt), "primary")] if kind == "delta" else []
    if history:
        support.commit_file(wt, "feature.py", "print('fixed')\n", "repair")
    payload = APPROVE if verdict == 0 else CHANGES
    payload = payload if findings is None else dict(payload, findings=findings)
    head = support.prepare_review(wt, gh, fake_bin, history=history, post_id=17, payload=payload)
    record_claude_runs(fake_bin)
    original = github._api

    def transport(endpoint, payload, **kwargs):
        response = original(endpoint, payload, **kwargs)
        if not endpoint.endswith("/reviews"):
            return response
        rows = [*history, published_review(head, payload["body"])]
        comments = [dict(c, commit_id=head, user={"login": LOGIN}, pull_request_review_id=17)
                    for c in payload["comments"]]
        if mutate:
            mutate(rows, comments)
        gh.respond(reviews_key(), rows)
        gh.respond(REVIEW_COMMENTS, comments)
        raise {
            "interrupt": KeyboardInterrupt(),
            "timeout": _proc.ProcTimeout("gh POST timed out"),
            "lost-reply": github.GhError(("gh", "api", endpoint), "client timed out after acceptance"),
        }[failure]

    monkeypatch.setattr(github, "_api", transport)
    return head


@pytest.fixture
def interrupt_review(wt, gh, fake_bin, monkeypatch, capsys):
    def interrupt(*, kind="primary", verdict=0, **options):
        head = lost_reply(wt, gh, fake_bin, monkeypatch, kind=kind, verdict=verdict,
                          failure="interrupt", **options)
        expect_cli(capsys, 130, "review")
        return head, cli._review_file(wt, head, kind)

    return interrupt


@pytest.fixture
def interrupted_receipt(interrupt_review):
    original = github._api
    head, path = interrupt_review()
    return head, path, json.loads(path.read_text()), original


def legacy_settings(label, requested, reported):
    return (f"Settings per native call ({label}, not backend attestation):\n"
            f"- Call 1: requested model {requested['model'] or 'unknown'} (effort {requested['effort'] or 'unknown'}); "
            f"CLI-reported model {reported['model'] or 'unknown'} (effort {reported['effort'] or 'unknown'})")


def raw_details(raw):
    return f"\n\n<details><summary>Raw reviewer output</summary>\n\n```\n{raw}\n```\n</details>"


def write_count(gh):
    return sum("--input" in call for call in gh.calls)


def assert_single_publication(gh, fake_bin, *, allow_body_update=False):
    assert (fake_bin / "claude.runs").read_text().splitlines() == ["run"]
    writes = [c["argv"][1] for c in gh.calls if "--input" in c]
    assert (writes.count(support.post_key().split()[1]) if allow_body_update else len(writes)) == 1


@pytest.mark.parametrize("override", [None, "pr", "head", "raw", "bytes", "effort"])
def test_plain_review_surfaces_receipt_after_interruption_before_post(wt, gh, fake_bin, monkeypatch, capsys, override):
    head = support.prepare_review(wt, gh, fake_bin)
    record_claude_runs(fake_bin)
    with monkeypatch.context() as before_post:
        before_post.setattr(github, "post_review", Mock(side_effect=KeyboardInterrupt))
        assert run(capsys, "review")[0] == 130

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
        assert_single_publication(gh, fake_bin, allow_body_update=True)
    else:
        assert code == 0, err
        assert (fake_bin / "claude.runs").read_text().splitlines() == ["run", "run"]


def recover(capsys, failure, verdict, *, stdin=None):
    if failure == "interrupt":
        expect_cli(capsys, 130, "review")
        if stdin:
            stdin.unlink()
        out, _ = expect_cli(capsys, verdict, "review", "--repost")
        assert stdin is None or not stdin.exists(), "recovery must not launch a reviewer"
    else:
        out, _ = expect_cli(capsys, verdict, "review")
        assert out.startswith("reviewer paths pinned to origin/main@")
    if verdict == 1:
        assert len(out.splitlines()) == int(failure != "interrupt")
    return out


@pytest.mark.parametrize("kind,verdict,failure", [
    (kind, verdict, failure) for kind in ("primary", "delta") for verdict in (0, 5)
    for failure in ("lost-reply", "interrupt")
] + [("delta", 5, "timeout")])
def test_accepted_publication_recovers_saved_verdict(wt, gh, fake_bin, monkeypatch, capsys, kind, verdict, failure):
    lost_reply(wt, gh, fake_bin, monkeypatch, kind=kind, verdict=verdict, failure=failure)
    out = recover(capsys, failure, verdict, stdin=fake_bin / "claude.stdin")
    assert out.splitlines()[-1].startswith(f"{kind} review by claude")
    assert_single_publication(gh, fake_bin)
    if verdict == 5:
        files = ["api", f"repos/{REPO}/pulls/7/files"]
        assert sum(call["argv"][:2] == files for call in gh.calls) == 2


@pytest.mark.parametrize("change", ["body", "comment", "location", "publisher", "pending",
                                     "dismissed", "newer", "duplicates", "bad_page", "auth", "quota",
                                     "head", "comment_page", "kind", "wrong_head"])
@pytest.mark.parametrize("failure", ["lost-reply", "interrupt"])
def test_recovery_requires_exact_live_publication(wt, gh, fake_bin, monkeypatch, capsys, change, failure):
    def mutate(rows, comments):
        row = rows[-1]
        edits = {
            "body": (row, {"body": row["body"] + "\nchanged"}),
            "comment": (comments[0], {"body": comments[0]["body"] + "changed"}),
            "location": (comments[0], {"line": 99}),
            "publisher": (row["user"], {"login": "another-publisher"}),
            "pending": (row, {"state": "PENDING"}),
            "dismissed": (row, {"state": "DISMISSED"}),
            "kind": (row, {"body": row["body"].replace("kind=delta", "kind=primary")}),
            "wrong_head": (row, {"commit_id": "b" * 40}),
        }
        if change in edits:
            target, update = edits[change]
            target.update(update)
        elif change in {"newer", "duplicates"}:
            state = "PENDING" if change == "newer" else row["state"]
            rows.append(dict(row, id=18, state=state))
            gh.respond(f"api repos/{REPO}/pulls/7/reviews/18/comments --method GET",
                       [{**c, "pull_request_review_id": 18} for c in comments])
        elif change == "head":
            arm_pr(gh, "b" * 40)
        elif change == "comment_page":
            comments[:] = comments * 100
            gh.respond(f"api repos/{REPO}/pulls/7/reviews/17/comments page=2", None)
        elif change in {"bad_page", "auth", "quota"}:
            rows[:] = [copy.deepcopy(rows[-1]) for _ in range(100)]
            if error := {"auth": "not logged in", "quota": "HTTP 429"}.get(change):
                gh.fail(reviews_key(2), error)
            else:
                gh.respond(reviews_key(2), None)
    lost_reply(wt, gh, fake_bin, monkeypatch, kind="delta", verdict=5, mutate=mutate, failure=failure)
    recover(capsys, failure, 5 if change == "duplicates" else 1)
    assert write_count(gh) == 1


@pytest.mark.parametrize("change", ["login", "raw", "repo", "publisher", "number"])
def test_interrupted_publication_uses_valid_saved_runner_and_request_identity(
    wt, gh, interrupt_review, capsys, change,
):
    if change == "login":
        support.commit_file(wt, "workflow.toml", support.WORKFLOW_BASE + f'review_publishers = ["{LOGIN}", "other-login"]\n', "publishers")
        git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    _, path = interrupt_review(kind="delta", verdict=5)
    saved = json.loads(path.read_text())
    if change == "login":
        gh.respond("api graphql viewer", {"data": {"viewer": {"login": "other-login"}}})
    elif change == "raw":
        saved["raw"] = "{}"
    else:
        saved["publication"][change] = "wrong"
    path.write_text(json.dumps(saved))
    expect_cli(capsys, 5 if change == "login" else 1, "review", "--repost")
    assert write_count(gh) == 1


@pytest.mark.parametrize("change", ["revoked", "deadline", "read_timeout"])
def test_recovery_cannot_outlive_deadline_or_live_review_evidence(
    wt, gh, fake_bin, monkeypatch, capsys, change,
):
    lost_reply(wt, gh, fake_bin, monkeypatch, kind="delta", verdict=5)
    original, monotonic = github.paged, github.time.monotonic
    def read(endpoint, *args, **kwargs):
        data = original(endpoint, *args, **kwargs)
        if endpoint.endswith("/comments"):
            if change == "revoked":
                rows = original(reviews_key().split()[1])
                rows[-1]["state"] = "DISMISSED"
                gh.respond(reviews_key(), rows)
            elif change == "deadline":
                monkeypatch.setattr(github.time, "monotonic", lambda: monotonic() + 31)
            else:
                raise _proc.ProcTimeout("read timed out")
        return data
    monkeypatch.setattr(github, "paged", read)
    out, _ = expect_cli(capsys, 1, "review", stderr_contains=("read-back blocked",))
    assert out.startswith("reviewer paths pinned to origin/main@") and len(out.splitlines()) == 1
    assert write_count(gh) == 1


def test_repost_recovers_review_with_redacted_findings(interrupt_review, gh, fake_bin, capsys):
    title = "Rotate sk-ant-FAKE0123"
    findings = [
        {"severity": "important", "path": "feature.py", "line": 1,
         "title": title, "body": "Remove sk-proj-Fake_only-0123 from settings."},
        {"severity": "suggestion", "path": None, "line": None,
         "title": "Replace ghp_FAKE0123456789", "body": "See sk-FakeToken0123 in the log."},
    ]
    head, _ = interrupt_review(verdict=5, findings=findings)
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
    head = lost_reply(wt, gh, fake_bin, monkeypatch, kind="primary", verdict=5, failure="interrupt")
    original = github._api
    def rejected(endpoint, payload, **kwargs):
        if endpoint.endswith("/reviews"):
            raise github.GhError(("gh", "api", endpoint), "explicit server rejection")
        return original(endpoint, payload, **kwargs)
    monkeypatch.setattr(github, "_api", rejected)
    expect_cli(capsys, 1, "review", stderr_contains=("GitHub rejected it and `--repost` resends the same review",))
    monkeypatch.setattr(github, "_api", original)
    assert run(capsys, "review", "--repost")[0] == 130
    (fake_bin / "claude.stdin").unlink()
    out, _ = expect_cli(capsys, 5, "review", "--repost")
    assert out.startswith("primary review by claude")
    assert not (fake_bin / "claude.stdin").exists()
    assert write_count(gh) == 1
    assert support.marker(head, "primary", "repost") in support.posted_review(gh)["body"]


@pytest.mark.parametrize("failure,budget", [("timeout", 0.5), ("transient", 0.5), ("transient", 10), ("write", 0.5)])
def test_recovery_transport_retries_only_within_deadline(monkeypatch, failure, budget):
    sleeps = []
    request = Mock(return_value=_proc.Completed(1, "", "HTTP 503", 0),
                   side_effect=_proc.ProcTimeout("timeout") if failure != "transient" else None)
    monkeypatch.setattr(github, "run", request)
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(github.time, "monotonic", lambda: 10.0 + sum(sleeps) + int(failure == "write" and bool(request.call_count)))
    error = github.GhError if failure == "transient" else _proc.ProcTimeout
    with pytest.raises(error), github.bounded_reads(10 + budget, retry=True):
        github.mark_ready("acme/widgets", 7) if failure == "write" else github.api_get("repos/acme/widgets/pulls/7/reviews")
    timeouts = [call.kwargs["timeout"] for call in request.call_args_list]
    assert (timeouts, sleeps) == (([10, 5], [5]) if budget == 10 else ([0.5], []))


@pytest.mark.parametrize("optional", ["junk", [], [{"model": "request"}] * 5])
def test_optional_settings_cannot_block_explicit_repost(interrupted_receipt, gh, fake_bin, monkeypatch, capsys, optional):
    _, path, saved, original = interrupted_receipt
    saved["provenance"]["requested_settings"] = optional
    saved["provenance"]["reported_settings"] = "forged"
    path.write_text(json.dumps(saved))
    quoting = {"commit_id": saved["head"], "state": "COMMENTED", "user": {"login": "some-human"}}  # r7-g4-3
    gh.respond(reviews_key(), [quoting | {"body": "I do not see the `loopzero:review` marker yet."}])
    monkeypatch.setattr(github, "_api", original)
    expect_cli(capsys, 0, "review", "--repost")
    body = support.posted_review(gh)["body"]
    assert "CLI-reported model unknown (effort unknown)" in body and "forged" not in body
    assert (fake_bin / "claude.runs").read_text().splitlines() == ["run"]


@pytest.mark.parametrize("published", [True, False])
def test_repost_confirms_published_legacy_fallback_note_but_never_posts_one(
    wt, gh, fake_tool, interrupt_review, capsys, published,
):
    legacy = "independent reviewers failed: codex: /home/u/secret: boom"
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    fake_tool("codex", "if [ \"$1\" = --version ]; then echo 'codex-cli 0.161.0'; exit 0; fi\n"
              "echo 'Please run codex login' >&2\nexit 1\n")

    redacted = "independent reviewers failed: codex: not authenticated"

    def mutate(rows, _):
        review = rows.pop()
        if published:
            rows.append(dict(review, body=review["body"].replace(redacted, legacy)))

    _, receipt = interrupt_review(mutate=mutate)
    data = json.loads(receipt.read_text())
    assert data["fallback"] == redacted
    receipt.write_text(json.dumps({**data, "fallback": legacy}))
    errors = () if published else ("not in the published form",)
    expect_cli(capsys, 0 if published else 1, "review", "--repost", stderr_contains=errors)
    assert write_count(gh) == 1


@pytest.mark.parametrize("publication,source,metadata", [
    *((fmt, "model", None) for fmt in ("pre-configured", "transcript", "summary")),
    *(("legacy", source, metadata) for source in ("model", "repost")
      for metadata in ("legacy", "null", "empty", "malformed")),
])
def test_repost_confirms_historical_claude_publication(
    interrupted_receipt, gh, fake_bin, capsys, publication, source, metadata,
):
    head, path, saved, _ = interrupted_receipt
    provenance = saved["provenance"]
    prefix = support.marker(head, "primary", source)
    qualified = publication != "legacy"
    identity = f"loopzero primary review by claude on `{head}`, "
    verdict = f"{saved['duration_s']:.1f}s: **approve** (0 critical, 0 important, 0 suggestion)"
    if qualified:
        settings = "CLI claims" if publication == "pre-configured" else "launch configuration and CLI claims"
        body = f"{prefix}\n{identity}{verdict}\n\n" + legacy_settings(settings, provenance["requested_settings"][0], saved)
        if publication != "pre-configured":
            body += "; configured model unknown (effort unknown)"
    else:
        provenance["requested_settings"] = {"null": None, "empty": [], "malformed": "bad", "legacy": None}[metadata]
        if metadata == "legacy":
            del provenance["requested_settings"]
        path.write_text(json.dumps(saved))
        if source == "repost":
            prefix += "\nReposted from runner session: `claude-session-123`"
        effort = f" (effort {saved['effort']})" if saved["effort"] else ""
        body = f"{prefix}\n{identity}model {saved['model'] or 'unknown'}{effort}, {verdict}"
    if publication == "summary":
        body += "\n\nNative Claude output is retained in the restricted local review receipt."
    else:
        body += raw_details(saved["raw"])
    gh.respond(reviews_key(), [published_review(head, body)])
    if publication == "pre-configured":
        assert "configured_settings" in provenance
        assert run(capsys, "review", "--repost")[0] == 1
        provenance.pop("configured_settings")
        path.write_text(json.dumps(saved))
    out, err = expect_cli(capsys, int(not qualified), "review", "--repost")
    if qualified:
        assert saved["raw"] not in out + err
    if publication == "pre-configured":
        gh.respond(reviews_key(), [published_review(head, body + "changed")])
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
        + legacy_settings("CLI claims", call["requested"], call["reported"])
        + codex_notice(evidence["raw_sha256"])
    )
    gh.respond(reviews_key(), [published_review(head, body + ("changed" if altered else ""), state="APPROVED")])
    gh.respond(REVIEW_COMMENTS, [])

    monkeypatch.setattr(cli.runners, "review_with", lambda *a, **k: pytest.fail("repost launched a reviewer"))
    _, err = expect_cli(capsys, int(altered), "review", "--repost")
    if altered:
        assert "conflicting or ambiguous review publication" in err
    assert all("--input" not in call for call in gh.calls)


def codex_notice(raw_sha256):
    return raw_details("Native Codex transcript retained in the restricted local review receipt; "
                       "public evidence is publisher-derived, not backend attestation. "
                       f"Exact raw SHA256: {raw_sha256}")


def test_old_notice_heading_is_normalized_only_for_the_trailing_notice():
    quoted = raw_details("Native Codex transcript quoted")
    notice = codex_notice("a" * 64)
    new = notice.replace("Raw reviewer output", "Review evidence notice")
    assert cli._comparable_body("x" + quoted + notice) == "x" + quoted + new
    assert cli._comparable_body("x" + notice + quoted) == "x" + notice + quoted
