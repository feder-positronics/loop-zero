"""Codex's bounded public proof derives from complete private native streams."""

import base64
import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from loopzero import cli, eligibility, github, runners
from loopzero.types import CheckReport, Config
from tests import test_cli, test_runners
from tests.conftest import git

fake_bwrap = test_runners.fake_bwrap
gh = test_cli.gh
APPROVE = test_runners.APPROVE


@pytest.fixture
def native(git_repo, fake_bin, fake_tool):
    base = git(git_repo, "rev-parse", "HEAD").strip()
    (git_repo / "large.txt").write_text(
        "PRIVATE_CANARY /home/private/secret.txt\n" + "x" * 40000 + "Ω\n"
    )
    git(git_repo, "add", "large.txt")
    git(git_repo, "commit", "-qm", "large synthetic source")
    head = git(git_repo, "rev-parse", "HEAD").strip()
    patch = git(git_repo, "diff", "--no-color", base, head).strip()
    script = """#!/usr/bin/env python3
import json,pathlib,subprocess,sys,uuid
args=sys.argv[1:]
if args == ['--version']: print('codex-cli 0.164.0');sys.exit()
if args == ['exec','--help']: print('--ignore-user-config');sys.exit()
cwd=args[args.index('--cd')+1]
session='session-'+str(uuid.uuid4());usage={'input_tokens':1,'output_tokens':2}
print(json.dumps({'type':'thread.started','thread_id':session,'session_id':session}))
for index,argv in enumerate([['git','-C',cwd,'rev-parse','HEAD'],['git','-C',cwd,'diff','--no-color',BASE,HEAD]]):
 command=' '.join(argv);item={'id':'read-'+str(index),'type':'command_execution','command':command}
 print(json.dumps({'type':'item.started','item':dict(item,status='in_progress')}))
 done=subprocess.run(argv,capture_output=True,text=True)
 print(json.dumps({'type':'item.completed','item':dict(item,status='completed',exit_code=done.returncode,aggregated_output=done.stdout)}))
message=json.dumps({'verdict':'approve','findings':[]})
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(message)
print(json.dumps({'type':'item.completed','item':{'id':'final','type':'agent_message','text':message}}))
print(json.dumps({'type':'turn.completed','usage':usage}))
print('model: gpt-6-astra\\nreasoning effort: medium',file=sys.stderr)
""".replace(",BASE,HEAD", "," + repr(base) + "," + repr(head))
    fake_tool("codex", script)
    context = {
        "repo": "acme/widgets",
        "pr": 7,
        "publisher": "bot",
        "head": head,
        "kind": "primary",
        "base": base,
        "diff_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "diff_bytes": len(patch.encode()),
    }

    def invoke(chunk=None):
        return runners.review_with(
            "codex",
            cwd=git_repo,
            head=head,
            kind="primary",
            diff=patch,
            task_text="Synthetic source inspection",
            model="gpt-6-astra",
            effort="medium",
            review_context=context,
            chunk=chunk,
        )

    return invoke, context, git_repo


def body(result):
    prefix = cli.review_marker(result.head, result.kind)
    return github._review_body(result, [], prefix, publication=("acme/widgets", 7))


def row(result, rendered=None):
    return {
        "id": 100,
        "commit_id": result.head,
        "user": {"login": "bot"},
        "state": "COMMENTED",
        "body": rendered if rendered is not None else body(result),
    }


def rewrite(result, change):
    decoded = json.loads(result.raw)
    change(decoded)
    return dataclasses.replace(result, raw=json.dumps(decoded))


def packed(value):
    token = (
        base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    return runners.CODEX_HEADER + token + " -->"


def test_large_real_native_private_public_saved_and_exact_bytes(native, tmp_path):
    result = native[0]()
    assert len(result.raw.encode()) > 30000
    evidence = runners.codex_evidence(result)
    assert evidence["complete"] and evidence["calls"][0]["inspection"] == "verified"
    assert evidence["calls"][0]["reported"] == {"model": "gpt-6-astra", "effort": "medium"}
    assert evidence["raw_sha256"] == hashlib.sha256(result.raw.encode()).hexdigest()
    assert (
        evidence["raw_sha256"]
        != hashlib.sha256(json.dumps(json.loads(result.raw), indent=2).encode()).hexdigest()
    )
    rendered = body(result)
    lines = rendered.splitlines()
    assert lines[0] == cli.review_marker(result.head, "primary") and lines[1].startswith(
        runners.CODEX_HEADER
    )
    assert all(
        secret not in rendered
        for secret in ("PRIVATE_CANARY", "/home/private", "git -C", str(native[2]))
    )
    parsed = eligibility.review_projection(row(result), result.head, "acme/widgets", 7)
    assert parsed == (True, evidence)
    assert eligibility.eligible_review(
        [row(result)], result.head, ("bot",), repo="acme/widgets", number=7
    )
    cli._save_review(tmp_path, result)
    path = tmp_path / evidence["receipt"]
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    loaded = cli._load_review(tmp_path, result.head, "primary")
    assert loaded.raw == result.raw and body(loaded) == rendered
    assert json.loads(path.read_text())["raw"] == result.raw


@pytest.mark.parametrize(
    "mutation",
    [
        "fatal_before",
        "fatal_after",
        "missing_terminal",
        "wrong_final",
        "mixed_session",
        "wrong_repo",
        "wrong_cd",
        "unbound_repo",
        "quoted_command",
        "ambiguous_shell",
        "moving_range",
        "failed",
        "echo",
        "unmatched",
        "wrong_head",
        "wrong_diff",
        "duplicate_event",
        "after_verdict",
        "terminal_failure",
    ],
)
def test_full_native_attribution_and_fatal_controls(native, mutation):
    result = native[0]()
    data = json.loads(result.raw)
    events = data["events"]
    fatal = {
        "type": "item.completed",
        "item": {
            "id": "fatal",
            "type": "error",
            "message": "Code Mode is unavailable because failed to spawn code-mode host /different/release/codex-code-mode-host: host executable was not found. Code mode will fail closed; install helper.",
        },
    }
    if mutation.startswith("fatal_"):
        events.insert(1 if mutation == "fatal_before" else -2, fatal)
    if mutation == "missing_terminal":
        events.pop()
    if mutation == "after_verdict":
        events.insert(1, events.pop(-2))
    if mutation == "terminal_failure":
        events.insert(-1, {"type": "turn.failed"})
    if mutation == "duplicate_event":
        for event in events:
            if event.get("item", {}).get("type") == "command_execution":
                event["item"]["id"] = "same"
    if mutation == "wrong_final":
        events[-2]["item"]["text"] = '{"verdict":"request_changes","findings":[]}'
    if mutation == "mixed_session":
        events[2]["session_id"] = "another-session"
    for event in events:
        item = event.get("item", {})
        if item.get("type") != "command_execution":
            continue
        if mutation == "wrong_repo":
            item["command"] = item["command"].replace(str(native[2]), "/wrong/repo")
        if mutation == "wrong_cd":
            item["command"] = item["command"].replace(
                "git -C " + str(native[2]), "cd /wrong/repo && git"
            )
        if mutation == "unbound_repo":
            item["command"] = item["command"].replace("git -C " + str(native[2]), "git")
        if mutation == "quoted_command":
            item["command"] = "'" + item["command"] + "'"
        if mutation == "ambiguous_shell":
            item["command"] += " ; echo ignored"
        if mutation == "moving_range":
            item["command"] = item["command"].replace(native[1]["base"], "origin/main")
        if mutation == "failed" and event["type"] == "item.completed":
            item["exit_code"] = 1
        if mutation == "echo":
            item["command"] = "echo " + item["command"]
        if mutation == "unmatched" and event["type"] == "item.started":
            item["id"] = "not-" + item["id"]
        if mutation == "wrong_head" and "aggregated_output" in item:
            item["aggregated_output"] = item["aggregated_output"].replace(result.head, "a" * 40)
        if mutation == "wrong_diff" and "aggregated_output" in item:
            item["aggregated_output"] = item["aggregated_output"].replace(
                "PRIVATE_CANARY", "FAKE_CANARY"
            )
    altered = dataclasses.replace(result, raw=json.dumps(data))
    if mutation in (
        "fatal_before",
        "fatal_after",
        "missing_terminal",
        "wrong_final",
        "mixed_session",
        "after_verdict",
        "terminal_failure",
    ):
        assert not runners.validate_runner_result(altered)
        with pytest.raises(runners.RunnerBadOutput):
            runners.codex_evidence(altered)
        with pytest.raises(runners.RunnerBadOutput):
            body(altered)
    else:
        evidence = runners.codex_evidence(altered)
        assert evidence["calls"][0]["inspection"] == "unknown"


@pytest.mark.parametrize(
    "mutation",
    ["valid", "missing", "reorder", "duplicate_session", "range", "context", "chunk_index"],
)
def test_complete_ordered_native_chunks(native, mutation):
    first, second = native[0]((1, 2)), native[0]((2, 2))
    merged = runners.merge_reviews([first, second])
    data = json.loads(merged.raw)
    if mutation == "missing":
        data.pop()
    if mutation == "reorder":
        data.reverse()
    if mutation == "duplicate_session":
        data[1]["session_id"] = data[0]["session_id"]
    if mutation == "range":
        data[1]["review_context"]["base"] = "f" * 40
    if mutation == "context":
        data[1].pop("review_context")
    if mutation == "chunk_index":
        data[1]["review_context"]["index"] = 1
    changed = dataclasses.replace(merged, raw=json.dumps(data))
    if mutation == "valid":
        evidence = runners.codex_evidence(changed)
        assert [c["index"] for c in evidence["calls"]] == [1, 2]
        assert all(c["inspection"] == "verified" for c in evidence["calls"])
        assert len({c["session"] for c in evidence["calls"]}) == 2
        assert eligibility.review_projection(row(changed), changed.head, "acme/widgets", 7) == (
            True,
            evidence,
        )
    else:
        assert not runners.validate_runner_result(changed)
        with pytest.raises(runners.RunnerBadOutput):
            runners.codex_evidence(changed)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "version",
        "bool_count",
        "float_pr",
        "bad_ref",
        "bad_digest",
        "offset",
        "missing_call",
        "session",
        "kind",
        "repo",
        "pr",
        "publisher",
        "head",
        "complete",
        "fatal",
        "duplicate_header",
        "before_marker",
        "after_prose",
        "noncanonical",
        "duplicate_key",
        "oversized",
    ],
)
def test_strict_transport_and_no_older_approval_revival(native, mutation):
    result = native[0]()
    rendered = body(result)
    line = rendered.splitlines()[1]
    value = runners.decode_codex_evidence(line)
    if mutation == "unknown_field":
        value["extra"] = 1
    if mutation == "version":
        value["version"] = 2
    if mutation == "bool_count":
        value["chunk_count"] = True
    if mutation == "float_pr":
        value["pr"] = 7.0
    if mutation == "bad_ref":
        value["receipt"] = "/private/review.json"
    if mutation == "bad_digest":
        value["raw_sha256"] = "A" * 64
    if mutation == "offset":
        value["calls"][0]["head_event"]["start"] = -1
    if mutation == "missing_call":
        value["calls"] = []
    if mutation == "session":
        value["calls"][0]["session"] = None
    if mutation == "kind":
        value["kind"] = "delta"
    if mutation == "repo":
        value["repo"] = "another/repo"
    if mutation == "pr":
        value["pr"] = 8
    if mutation == "publisher":
        value["publisher"] = "stranger"
    if mutation == "head":
        value["head"] = "f" * 40
    if mutation == "complete":
        value["complete"] = False
    if mutation == "fatal":
        value["calls"][0]["fatal"] = "present"
    changed = rendered.replace(line, packed(value))
    if mutation == "duplicate_header":
        changed = rendered.replace(line, line + "\n" + line)
    if mutation == "before_marker":
        changed = line + "\n" + rendered.replace(line + "\n", "")
    if mutation == "after_prose":
        changed = rendered.replace(line + "\n", "") + "\n" + line
    if mutation == "noncanonical":
        changed = rendered.replace(line, line[:-4] + "= -->")
    if mutation == "duplicate_key":
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).replace(
            '"version":1', '"version":1,"version":1'
        )
        changed = rendered.replace(
            line,
            runners.CODEX_HEADER
            + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
            + " -->",
        )
    if mutation == "oversized":
        changed = rendered.replace(line, runners.CODEX_HEADER + "x" * 8192 + " -->")
    prior = row(result)
    latest = row(result, changed)
    found = eligibility.eligible_review(
        [prior, latest], result.head, ("bot",), repo="acme/widgets", number=7
    )
    if mutation == "after_prose":
        assert eligibility.review_projection(latest, result.head, "acme/widgets", 7) == (
            False,
            None,
        )
        # Ordinary clean legacy is UNKNOWN; quoted projection is never inspection proof.
        assert found is not None
    else:
        assert found is None


def test_overflow_is_hold_without_silent_chunk_truncation(native):
    value = runners.codex_evidence(native[0]())
    value["calls"] = [
        {**copy.deepcopy(value["calls"][0]), "index": i, "session": "s" + str(i)}
        for i in range(1, 40)
    ]
    value["chunk_count"] = len(value["calls"])
    assert runners.valid_codex_evidence(value)
    with pytest.raises(runners.RunnerBadOutput, match="8192"):
        runners.encode_codex_evidence(value)


def test_private_audit_task_is_retained(native, tmp_path, monkeypatch, capsys):
    result = native[0]()
    cli._save_review(tmp_path, result)
    monkeypatch.setattr(cli, "_repo_root", lambda *_: pytest.fail("audit task must not be removed"))
    cli._cleanup(tmp_path)
    assert (tmp_path / ".loopzero" / f"review-{result.head[:12]}-primary.json").exists()
    assert "verified archive handoff" in capsys.readouterr().out


@pytest.mark.parametrize(
    "kind", ["normal", "repost", "tampered_digest", "changed_raw", "delta", "delta_wrong_range"]
)
def test_large_publication_confirmation_and_hosted_agreement(
    native, gh, tmp_path, fake_bin, monkeypatch, kind
):
    from tests.test_github import pr_json, rest_pr_json, threads_json

    result = native[0]()
    repo = "acme/widgets"
    cfg = Config(repo, "main", (), (), "squash", ("codex",), review_publishers=("bot",))
    if kind.startswith("delta"):
        reviewed, merge_base = result.head, native[1]["base"]
        (native[2] / "delta.txt").write_text("delta only\n")
        git(native[2], "add", "delta.txt")
        git(native[2], "commit", "-qm", "delta repair")
        head = git(native[2], "rev-parse", "HEAD").strip()
        git(native[2], "update-ref", "refs/remotes/origin/main", merge_base)
        task = native[2] / ".loopzero/task.md"
        task.parent.mkdir()
        task.write_text("Inspect the delta")
        tool = fake_bin / "codex"
        selected = merge_base if kind == "delta_wrong_range" else reviewed
        old_range = f"{merge_base!r},{reviewed!r}"
        new_range = f"{selected!r},{head!r}"
        tool.write_text(tool.read_text().replace(old_range, new_range))
        result = cli._run_review(
            native[2], cfg, head, "delta", reviewed, publication=(repo, 7, "bot")
        )
        proof = runners.codex_evidence(result)
        assert proof["kind"] == "delta" and proof["calls"][0]["base"] == reviewed != merge_base
        delta_diff = git(native[2], "diff", "--no-color", reviewed, head).strip().encode()
        assert proof["calls"][0]["diff_sha256"] == hashlib.sha256(delta_diff).hexdigest()
        assert eligibility.recovery_inspection(proof, head) is (kind == "delta")
        # Synthetic target routing only; the93 owning cases retain exact historical pins.
        monkeypatch.setattr(eligibility, "recovery_target", lambda *_: head)
        monkeypatch.setitem(eligibility._RECOVERY_TARGETS, 7, head)
        monkeypatch.setattr(eligibility, "recovery_reviews", lambda *_: (999,))
    head = result.head
    prefix = (
        cli._repost_prefix(result) if kind == "repost" else cli.review_marker(head, result.kind)
    )
    prefix += eligibility.recovery_suffix(repo, 7, head, [])
    gh.respond(f"api repos/{repo}/pulls/7/files", [])
    rendered = github.review_payload(repo, 7, head, result, prefix)["body"]
    review = row(result, rendered)
    if kind == "tampered_digest":
        line = rendered.splitlines()[1]
        value = runners.decode_codex_evidence(line)
        value["raw_sha256"] = "f" * 64
        review["body"] = rendered.replace(line, packed(value))
    if kind == "changed_raw":
        result = dataclasses.replace(result, raw=json.dumps(json.loads(result.raw), indent=2))
    gh.respond(
        f"api repos/{repo}/pulls/7",
        rest_pr_json(pr_json(headRefOid=head, mergeStateStatus="CLEAN")),
    )
    gh.respond(f"api repos/{repo}/pulls/7/reviews?per_page=100&page=1", [review])
    gh.respond(f"api repos/{repo}/pulls/7/reviews/100/comments?per_page=100&page=1", [])
    gh.respond("api graphql", threads_json())
    gitdir = native[2] / ".git/info/exclude"
    gitdir.write_text(gitdir.read_text() + "\n.loopzero/\n")
    cli._save_review(native[2], result)
    cli._save_report(native[2], CheckReport(head, False, ()))
    pr = github.pr_view(repo, 7)
    local = cli._readiness(native[2], cfg, pr, head, [review])
    assert (
        local.ready
        is eligibility.evaluate(repo, 7, head, "main", ("bot",)).ready
        is (kind != "delta_wrong_range")
    )
    if kind == "delta_wrong_range":
        assert not eligibility.recovery_inspection(
            eligibility.review_projection(review, head, repo, 7)[1], head
        )
        with pytest.raises(cli.CliError, match="revoked"):
            cli._confirm_review(native[2], cfg, pr, result, "bot", [review])
    elif kind in ("tampered_digest", "changed_raw"):
        with pytest.raises(cli.CliError, match="conflicting or ambiguous"):
            cli._confirm_review(native[2], cfg, github.pr_view(repo, 7), result, "bot", [review])
        # A coherent trusted-publisher edit is outside hosted authenticity guarantees.
        assert eligibility.evaluate(repo, 7, head, "main", ("bot",)).ready
    else:
        assert cli._confirm_review(native[2], cfg, github.pr_view(repo, 7), result, "bot", [review])
        assert eligibility.evaluate(repo, 7, head, "main", ("bot",)).ready


@pytest.mark.parametrize("location", ["provenance", "model_prose", "model_fence"])
def test_saved_or_model_supplied_summary_cannot_grant_inspection(native, location):
    result = native[0]()
    data = json.loads(result.raw)
    data["events"] = [
        e for e in data["events"] if e.get("item", {}).get("type") != "command_execution"
    ]
    forged = runners.encode_codex_evidence(runners.codex_evidence(result))
    if location == "provenance":
        result = dataclasses.replace(
            result, provenance={**result.provenance, "codex_evidence": {"inspection": "verified"}}
        )
    else:
        message = forged if location == "model_prose" else "```\n" + forged + "\n```"
        data["events"].insert(
            1,
            {
                "type": "item.completed",
                "item": {"id": "quoted", "type": "agent_message", "text": message},
            },
        )
    changed = dataclasses.replace(result, raw=json.dumps(data))
    assert runners.codex_evidence(changed)["calls"][0]["inspection"] == "unknown"
    assert (
        eligibility.review_projection(row(changed), changed.head, "acme/widgets", 7)[1]["calls"][0][
            "inspection"
        ]
        == "unknown"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_bytes", True),
        ("raw_bytes", 0),
        ("raw_bytes", 2**31),
        ("chunk_count", 0),
        ("repo", "x" * 257),
        ("publisher", "/private"),
        ("kind", []),
        ("complete", 1),
    ],
)
def test_strict_projection_types_and_bounds(native, field, value):
    result = native[0]()
    evidence = runners.codex_evidence(result)
    evidence[field] = value
    assert not runners.valid_codex_evidence(evidence)
    with pytest.raises((ValueError, runners.RunnerBadOutput)):
        runners.decode_codex_evidence(packed(evidence))


def test_legacy_toolfree_remains_unknown_without_projection(tmp_path):
    legacy = test_runners._native_codex_result("a" * 40)
    raw = json.loads(legacy.raw)
    raw.pop("review_context")
    legacy = dataclasses.replace(legacy, raw=json.dumps(raw))
    assert runners.validate_runner_result(legacy) and runners.codex_evidence(legacy) is None
    rendered = body(legacy)
    assert runners.CODEX_HEADER not in rendered
    assert eligibility.eligible_review(
        [row(legacy)], legacy.head, ("bot",), repo="acme/widgets", number=7
    )
    cli._save_review(tmp_path, legacy)
    assert cli._load_review(tmp_path, legacy.head, "primary").raw == legacy.raw


@pytest.mark.parametrize(
    "change",
    [
        "prose",
        "fence",
        "wrong_adjudication",
        "unknown_leading",
        "whitespace_json",
        "oversized_model",
        "oversized_effort",
        "invalid_event_offset",
    ],
)
def test_leading_block_is_anchored_and_schema_complete(native, change):
    result = native[0]()
    rendered = body(result)
    line = rendered.splitlines()[1]
    value = runners.decode_codex_evidence(line)
    if change == "prose":
        modified = rendered.replace(line + "\n", "") + "\n" + line
    elif change == "fence":
        modified = rendered.replace(line + "\n", "") + "\n```\n" + line + "\n```"
    elif change == "wrong_adjudication":
        modified = rendered.replace(
            line, line + "\n<!-- loopzero:adjudication v=1 replaces=1 target=" + ("f" * 40) + " -->"
        )
    elif change == "unknown_leading":
        modified = rendered.replace(line, line + "\n<!-- loopzero:unadopted v=1 -->")
    else:
        if change == "oversized_model":
            value["calls"][0]["reported"]["model"] = "m" * 129
        if change == "oversized_effort":
            value["calls"][0]["requested"]["effort"] = "e" * 33
        if change == "invalid_event_offset":
            value["calls"][0]["diff_event"]["end"] = value["raw_bytes"] + 1
        if change == "whitespace_json":
            token = (
                base64.urlsafe_b64encode(json.dumps(value, indent=2).encode()).decode().rstrip("=")
            )
            changed = runners.CODEX_HEADER + token + " -->"
        else:
            changed = packed(value)
        modified = rendered.replace(line, changed)
    present, evidence = eligibility.review_projection(
        row(result, modified), result.head, "acme/widgets", 7
    )
    assert evidence is None
    assert present is (change not in ("prose", "fence"))


def test_utf8_raw_is_hashed_exactly_and_local_replay_regenerates(native, tmp_path):
    result = native[0]()
    changed = dataclasses.replace(
        result, raw=json.dumps(json.loads(result.raw), ensure_ascii=False, indent=1)
    )
    assert len(changed.raw.encode()) > len(changed.raw)
    value = runners.codex_evidence(changed)
    assert value["raw_bytes"] == len(changed.raw.encode())
    assert value["raw_sha256"] == hashlib.sha256(changed.raw.encode()).hexdigest()
    assert value["raw_sha256"] != runners.codex_evidence(result)["raw_sha256"]
    cli._save_review(tmp_path, changed)
    loaded = cli._load_review(tmp_path, changed.head, changed.kind)
    assert loaded.raw == changed.raw and body(loaded) == body(changed)


def test_frozen_five_and_legacy_truncated_unknown_are_unchanged():
    fixture = Path(__file__).parent / "fixtures/review-runtime-20261027.json"
    assert (
        hashlib.sha256(fixture.read_bytes()).hexdigest()
        == "97ad4dd88afadbf1b663ae94078455a9d985b562c90f3e8168a7c21f7dd05d23"
    )
    head = "a" * 40
    legacy = {
        "id": 1,
        "commit_id": head,
        "state": "COMMENTED",
        "user": {"login": "bot"},
        "body": cli.review_marker(head, "primary")
        + "\n**approve**\n<details><summary>Raw reviewer output</summary>\n\n```\n[Transcript truncated] not JSON\n```\n</details>",
    }
    assert eligibility.review_projection(legacy, head, "acme/widgets", 7) == (False, None)
    assert (
        eligibility.eligible_review([legacy], head, ("bot",), repo="acme/widgets", number=7)
        is legacy
    )


@pytest.mark.parametrize("identity", ["another-session", None, {}])
def test_contradictory_native_session_fields(native, identity):
    result = native[0]()
    data = json.loads(result.raw)
    data["events"][2].update(thread_id=data["session_id"], session_id=identity)
    altered = dataclasses.replace(result, raw=json.dumps(data))
    assert not runners.validate_runner_result(altered)
    with pytest.raises(runners.RunnerBadOutput):
        runners.codex_evidence(altered)
    with pytest.raises(runners.RunnerBadOutput):
        body(altered)


def test_two_complete_sessions_cannot_combine_partial_inspection(native, fake_bin):
    tool = fake_bin / "codex"
    source = tool.read_text()
    partial = []
    for index in range(2):
        tool.write_text(source.replace(" command=", f" if index != {index}: continue\n command="))
        partial.append(native[0]((index + 1, 2)))
    merged = runners.merge_reviews(partial)
    proof = runners.codex_evidence(merged)
    assert proof["complete"] and len({call["session"] for call in proof["calls"]}) == 2
    assert all(call["inspection"] == "unknown" for call in proof["calls"])
    assert not eligibility.recovery_inspection(proof, merged.head)
    public = eligibility.review_projection(row(merged), merged.head, "acme/widgets", 7)[1]
    assert public == proof and not eligibility.recovery_inspection(public, merged.head)


def test_entire_leading_block_budget_includes_adjudication(native, monkeypatch):
    result = native[0]()
    marker = cli.review_marker(result.head, result.kind)
    header = runners.encode_codex_evidence(runners.codex_evidence(result))
    suffix = "\n<!-- loopzero:adjudication v=1 replaces=1 target=" + result.head + " -->"
    limit = len((marker + "\n" + header).encode())
    oversized = marker + "\n" + header + suffix + "\n**approve**"
    monkeypatch.setattr(runners, "CODEX_HEADER_LIMIT", limit)
    assert eligibility.review_projection(
        row(result, oversized), result.head, "acme/widgets", 7
    ) == (True, None)
    with pytest.raises(runners.RunnerBadOutput, match="leading machine block"):
        github._review_body(result, [], marker + suffix)


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
@pytest.mark.parametrize("overflow", [0, 1, 2])
def test_raw_leading_utf8_span_at_limit_and_overflow(native, separator, overflow):
    result = native[0]()
    marker = cli.review_marker(result.head, result.kind)
    header = runners.encode_codex_evidence(runners.codex_evidence(result))
    adjudication = "<!-- loopzero:adjudication v=1 replaces=1 target=" + result.head + " -->"
    span = separator.join((marker, header, adjudication))
    padding = 8192 + overflow - len(span.encode("utf-8"))
    padded = marker[:-3] + " " * padding + marker[-3:]
    span = separator.join((padded, header, adjudication))
    assert len(span.encode("utf-8")) == 8192 + overflow
    rendered = span + separator + "**approve**"
    present, evidence = eligibility.review_projection(
        row(result, rendered), result.head, "acme/widgets", 7
    )
    assert present and (evidence is not None) is (overflow == 0)
    latest = eligibility.eligible_review(
        [row(result), row(result, rendered)], result.head, ("bot",), repo="acme/widgets", number=7
    )
    assert (latest is not None) is (overflow == 0)
    if separator == "\n":  # Publication stays LF; readers count every accepted separator verbatim.
        if overflow:
            with pytest.raises(runners.RunnerBadOutput, match="leading machine block"):
                github._review_body(result, [], padded + "\n" + adjudication)
        else:
            assert github._review_body(result, [], padded + "\n" + adjudication).startswith(padded)
