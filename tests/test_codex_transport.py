"""Codex's bounded public proof derives from complete private native streams."""

import base64
import dataclasses
import hashlib
import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from shutil import copytree

import pytest

from loopzero import cli, eligibility, github, runners
from loopzero.types import CheckReport, CheckResult, Config
from tests import test_cli, test_runners
from tests.conftest import commit_file, git, git_environment, review_summary

fake_bwrap = test_runners.fake_bwrap
gh = test_cli.gh
APPROVE = test_runners.APPROVE
SYNTHETIC_NATIVE = test_runners._native_codex_result("a" * 40)


@pytest.fixture(scope="session")
def native_history(git_repo_seed, tmp_path_factory):
    repo = copytree(git_repo_seed, tmp_path_factory.mktemp("native-history") / "repo")
    base = git(repo, "rev-parse", "HEAD").strip()
    content = "PRIVATE_CANARY /home/private/secret.txt\n" + "x" * 40000 + "Ω\n"
    commit_file(repo, "large.txt", content, "large synthetic source")
    head = git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head, git(repo, "diff", "--no-color", base, head).strip()


@pytest.fixture
def native(native_history, tmp_path, fake_tool):
    seed, base, head, patch = native_history
    git_repo = copytree(seed, tmp_path / "repo")
    script = """#!/usr/bin/env python3
import json,pathlib,subprocess,sys,uuid
args=sys.argv[1:]
if args == ['--version']: print('codex-cli 0.164.0');sys.exit()
cwd=args[args.index('--cd')+1]
session='session-'+str(uuid.uuid4());usage={'input_tokens':1,'output_tokens':2}
print(json.dumps({'type':'thread.started','thread_id':session,'session_id':session}))
for index,argv in enumerate([['git','-C',cwd,'rev-parse','HEAD'],['git','-C',cwd,'diff','--no-color',BASE,HEAD]]):
 command=' '.join(argv);item={'id':'read-'+str(index),'type':'command_execution','command':command}
 print(json.dumps({'type':'item.started','item':dict(item,status='in_progress')}))
 done=subprocess.run(argv,capture_output=True)
 print(json.dumps({'type':'item.completed','item':dict(item,status='completed',exit_code=done.returncode,aggregated_output=done.stdout.decode(errors="replace"))}))
message=json.dumps({'verdict':'approve','findings':[]})
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(message)
print(json.dumps({'type':'item.completed','item':{'id':'final','type':'agent_message','text':message}}))
print(json.dumps({'type':'turn.completed','usage':usage}))
print('model: gpt-6.2-astra\\nreasoning effort: medium',file=sys.stderr)
""".replace(",BASE,HEAD", "," + repr(base) + "," + repr(head))
    fake_tool("codex", script)
    encoded_patch = patch.encode()
    context = {
        "repo": "acme/widgets", "pr": 7, "publisher": "bot", "head": head,
        "kind": "primary", "base": base, "diff_bytes": len(encoded_patch),
        "diff_sha256": hashlib.sha256(encoded_patch).hexdigest(),
    }

    def invoke(chunk=None):
        return runners.review_with(
            "codex",
            cwd=git_repo,
            head=head,
            kind="primary",
            diff=patch,
            task_text="Synthetic source inspection",
            model="gpt-6.2-astra",
            effort="medium",
            review_context=context,
            chunk=chunk,
        )

    return invoke, context, git_repo


@pytest.fixture(scope="module")
def native_transcripts():
    return {}


@pytest.fixture
def recorded_native(request, native_transcripts):
    """Replay immutable runner output for tests that mutate transcript data."""
    def capture(chunk=None):
        if chunk not in native_transcripts:
            invoke, context, repo = request.getfixturevalue("native")
            native_transcripts[chunk] = invoke(chunk), context, repo
        return native_transcripts[chunk]

    return capture


def body(result):
    prefix = cli.review_marker(result.head, result.kind)
    return github._review_body(result, [], prefix, publication=("acme/widgets", 7))


def row(result, rendered=None):
    return {
        "id": 100, "commit_id": result.head, "user": {"login": "bot"}, "state": "COMMENTED",
        "body": rendered if rendered is not None else body(result),
    }


@pytest.fixture(scope="module")
def published_native():
    rendered = body(SYNTHETIC_NATIVE)
    return SYNTHETIC_NATIVE, rendered, rendered.splitlines()[1]


def projection(result, rendered=None):
    return eligibility.review_projection(row(result, rendered), result.head, "acme/widgets", 7)


def eligible(*reviews):
    return eligibility.eligible_review(
        list(reviews), reviews[-1]["commit_id"], ("bot",), repo="acme/widgets", number=7
    )


def assert_invalid_native(result):
    assert not runners.validate_runner_result(result)
    for inspect in (runners.codex_evidence, body):
        with pytest.raises(runners.RunnerBadOutput):
            inspect(result)


def rewrite(result, change):
    decoded = json.loads(result.raw)
    change(decoded)
    return dataclasses.replace(result, raw=json.dumps(decoded))


def packed_json(payload):
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return runners.CODEX_HEADER + token + " -->"


def packed(value):
    return packed_json(json.dumps(value, sort_keys=True, separators=(",", ":")))


@pytest.mark.parametrize("formatted", [False, True])
def test_large_real_native_private_public_saved_and_exact_bytes(native, formatted):
    invoke, _, git_repo = native
    result = invoke()
    if formatted:
        changed = dataclasses.replace(
            result, raw=json.dumps(json.loads(result.raw), ensure_ascii=False, indent=1)
        )
        assert len(changed.raw.encode()) > len(changed.raw)
        assert runners.codex_evidence(changed)["raw_sha256"] != runners.codex_evidence(result)["raw_sha256"]
        result = changed
    assert len(result.raw.encode()) > 30000
    evidence = runners.codex_evidence(result)
    assert evidence["complete"] and evidence["calls"][0]["inspection"] == "unknown"
    assert evidence["calls"][0]["reported"] == {"model": "gpt-6.2-astra", "effort": "medium"}
    assert evidence["raw_bytes"] == len(result.raw.encode())
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
        for secret in ("PRIVATE_CANARY", "/home/private", "git -C", str(git_repo))
    )
    assert projection(result) == (True, evidence)
    assert eligible(row(result))
    cli._save_review(git_repo, result)
    path = git_repo / evidence["receipt"]
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    loaded, _ = cli._load_review(git_repo, result.head, "primary")
    assert loaded.raw == result.raw and body(loaded) == rendered
    assert json.loads(path.read_text())["raw"] == result.raw


@pytest.mark.parametrize("mutation", [
    "fatal_before", "fatal_after", "missing_terminal", "wrong_final", "mixed_session",
    "wrong_repo", "wrong_cd", "unbound_repo", "quoted_command", "ambiguous_shell",
    "moving_range", "failed", "echo", "unmatched", "wrong_head", "wrong_diff",
    "duplicate_event", "after_verdict", "terminal_failure",
])
def test_full_native_attribution_and_fatal_controls(recorded_native, mutation):
    result, context, repo = recorded_native()
    data = json.loads(result.raw)
    events = data["events"]
    fatal = {
        "type": "item.completed",
        "item": {
            "id": "fatal", "type": "error",
            "message": "Code Mode is unavailable because failed to spawn code-mode host /different/release/codex-code-mode-host: host executable was not found. Code mode will fail closed; install helper.",
        },
    }
    if mutation.startswith("fatal_"):
        events.insert(1 if mutation == "fatal_before" else -2, fatal)
    elif mutation == "missing_terminal":
        events.pop()
    elif mutation == "after_verdict":
        events.insert(1, events.pop(-2))
    elif mutation == "terminal_failure":
        events.insert(-1, {"type": "turn.failed"})
    elif mutation == "wrong_final":
        events[-2]["item"]["text"] = '{"verdict":"request_changes","findings":[]}'
    elif mutation == "mixed_session":
        events[2]["session_id"] = "another-session"
    command_edits = {
        "wrong_repo": (str(repo), "/wrong/repo"),
        "wrong_cd": ("git -C " + str(repo), "cd /wrong/repo && git"),
        "unbound_repo": ("git -C " + str(repo), "git"),
        "moving_range": (context["base"], "origin/main"),
    }
    output_edits = {"wrong_head": (result.head, "a" * 40), "wrong_diff": ("PRIVATE_CANARY", "FAKE_CANARY")}
    command_formats = {"quoted_command": "'{}'", "ambiguous_shell": "{} ; echo ignored", "echo": "echo {}"}
    for event in events:
        item = event.get("item", {})
        if item.get("type") != "command_execution":
            continue
        if mutation == "duplicate_event":
            item["id"] = "same"
        elif mutation in command_edits:
            item["command"] = item["command"].replace(*command_edits[mutation])
        elif mutation in command_formats:
            item["command"] = command_formats[mutation].format(item["command"])
        elif mutation == "failed" and event["type"] == "item.completed":
            item["exit_code"] = 1
        elif mutation == "unmatched" and event["type"] == "item.started":
            item["id"] = "not-" + item["id"]
        elif mutation in output_edits and "aggregated_output" in item:
            item["aggregated_output"] = item["aggregated_output"].replace(*output_edits[mutation])
    altered = dataclasses.replace(result, raw=json.dumps(data))
    if mutation in (
        "fatal_before", "fatal_after", "missing_terminal", "wrong_final",
        "mixed_session", "after_verdict", "terminal_failure",
    ):
        assert_invalid_native(altered)
    else:
        evidence = runners.codex_evidence(altered)
        assert evidence["calls"][0]["inspection"] == "unknown"


@pytest.mark.parametrize("mutation", [
    "valid", "partial_reads", "missing", "reorder", "duplicate_session", "range", "context", "chunk_index", "wrong_head",
])
def test_complete_ordered_native_chunks(recorded_native, request, fake_bin, mutation):
    if mutation == "wrong_head":
        another_head = dataclasses.replace(SYNTHETIC_NATIVE, head="b" * 40)
        with pytest.raises(ValueError, match="chunk reviews do not describe the same review"):
            runners.merge_reviews([SYNTHETIC_NATIVE, another_head])
        return
    chunks = []
    if mutation == "partial_reads":
        invoke, _, _ = request.getfixturevalue("native")
        tool = fake_bin / "codex"
        script = tool.read_text()
        for index in range(2):
            tool.write_text(script.replace(" command=", f" if index != {index}: continue\n command="))
            chunks.append(invoke((index + 1, 2)))
    else:
        chunks = [recorded_native((index, 2))[0] for index in (1, 2)]
    merged = runners.merge_reviews(chunks)
    data = json.loads(merged.raw)
    edits = {
        "duplicate_session": (("session_id",), data[0]["session_id"]),
        "range": (("review_context", "base"), "f" * 40),
        "chunk_index": (("review_context", "index"), 1),
    }
    if mutation in edits:
        update_evidence(data[1], *edits[mutation])
    elif mutation == "context":
        data[1].pop("review_context")
    elif mutation == "missing":
        data.pop()
    elif mutation == "reorder":
        data.reverse()
    changed = dataclasses.replace(merged, raw=json.dumps(data))
    if mutation in {"valid", "partial_reads"}:
        evidence = runners.codex_evidence(changed)
        assert evidence["complete"]
        assert [c["index"] for c in evidence["calls"]] == [1, 2]
        assert all(c["inspection"] == "unknown" for c in evidence["calls"])
        assert len({c["session"] for c in evidence["calls"]}) == 2
        assert projection(changed) == (True, evidence)
    else:
        assert_invalid_native(changed)


def legacy_inspection(value):
    """Reviews published before inspection was retired carry verified event ranges."""
    value["calls"][0] |= {"inspection": "verified", "head_event": {"id": "item_1", "start": 0, "end": 40},
                          "diff_event": {"id": "item_2", "start": 40, "end": 80}}
    return value


def test_retire_inspection_respects_the_published_block_limit(published_native):
    _, rendered, line = published_native
    legacy_evidence = legacy_inspection(runners.decode_codex_evidence(line))
    assert runners.valid_codex_evidence(legacy_evidence)
    legacy = rendered.replace(line, packed(legacy_evidence))
    assert legacy != rendered and runners.retire_inspection(legacy) == rendered
    marker = legacy.split("\n", 1)[0]
    oversized = legacy.replace(marker, marker + " " * (runners.CODEX_HEADER_LIMIT - len(line)), 1)
    assert len(runners.leading_review_block(oversized)[1].encode()) > runners.CODEX_HEADER_LIMIT
    assert runners.retire_inspection(oversized) == oversized  # never shortened into validity
    assert cli._comparable_body(github.BODY_BLOCKERS + legacy) == github.BODY_BLOCKERS + rendered


def update_evidence(value, path, replacement):
    if "head_event" in path or "diff_event" in path:
        legacy_inspection(value)
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize("mutation", [
    "duplicate_header", "before_marker", "after_prose", "noncanonical", "duplicate_key", "oversized",
    "fence", "wrong_adjudication", "unknown_leading", "whitespace_json", "invalid_event_offset",
    (("extra",), 1),
    (("version",), 2),
    (("chunk_count",), True),
    (("pr",), 7.0),
    (("receipt",), "/private/review.json"),
    (("raw_sha256",), "A" * 64),
    (("calls", 0, "head_event", "start"), -1),
    (("calls",), []),
    (("calls", 0, "session"), None),
    (("kind",), "delta"),
    (("repo",), "another/repo"),
    (("pr",), 8),
    (("publisher",), "stranger"),
    (("head",), "f" * 40),
    (("complete",), False),
    (("calls", 0, "fatal"), "present"),
    (("calls", 0, "reported", "model"), "m" * 129),
    (("calls", 0, "requested", "effort"), "e" * 33),
])
def test_invalid_review_evidence_is_rejected(published_native, mutation):
    result, rendered, line = published_native
    value = runners.decode_codex_evidence(line)
    without_header = rendered.replace(line + "\n", "")
    replacement = {
        "duplicate_header": f"{line}\n{line}",
        "noncanonical": line[:-4] + "= -->",
        "oversized": runners.CODEX_HEADER + "x" * 8192 + " -->",
        "wrong_adjudication": f"{line}\n<!-- loopzero:adjudication v=1 replaces=1 target={'f' * 40} -->",
        "unknown_leading": f"{line}\n<!-- loopzero:unadopted v=1 -->",
        "whitespace_json": packed_json(json.dumps(value, indent=2)),
    }
    moved = {
        "before_marker": line + "\n" + without_header,
        "after_prose": without_header + "\n" + line,
        "fence": without_header + "\n```\n" + line + "\n```",
    }
    edit = (("calls", 0, "diff_event", "end"), value["raw_bytes"] + 1) if mutation == "invalid_event_offset" else mutation
    if isinstance(edit, tuple):
        update_evidence(value, *edit)
        changed = rendered.replace(line, packed(value))
    elif mutation == "duplicate_key":
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        changed = rendered.replace(line, packed_json(payload.replace('"version":1', '"version":1,"version":1')))
    elif mutation in moved:
        changed = moved[mutation]
    else:
        changed = rendered.replace(line, replacement[mutation])
    if mutation in ("after_prose", "fence"):
        assert projection(result, changed) == (False, None)
    elif mutation in ("wrong_adjudication", "unknown_leading", "whitespace_json", "invalid_event_offset") or (
        isinstance(mutation, tuple) and mutation[0] in (("calls", 0, "reported", "model"), ("calls", 0, "requested", "effort"))
    ):
        assert projection(result, changed) == (True, None)
    if isinstance(mutation, tuple) or mutation in (
        "duplicate_header", "before_marker", "after_prose", "noncanonical", "duplicate_key", "oversized",
    ):
        found = eligible(row(result), row(result, changed))
        # Clean legacy may remain eligible, but a quoted header supplies no inspection proof.
        assert (found is not None) is (mutation == "after_prose")


@pytest.mark.parametrize("valid,diagnostic", [
    (True, "8192"), (False, "invalid native evidence projection"),
], ids=["overflow", "wrong-schema"])
def test_codex_evidence_rejects_invalid_or_oversized_projections(valid, diagnostic):
    value = {"version": 2}
    if valid:
        value = runners.codex_evidence(SYNTHETIC_NATIVE)
        value["calls"] = [dict(value["calls"][0], index=i, session=f"s{i}") for i in range(1, 40)]
        value["chunk_count"] = len(value["calls"])
        assert runners.valid_codex_evidence(value)
    with pytest.raises(runners.RunnerBadOutput, match=diagnostic):
        runners.encode_codex_evidence(value)


@pytest.mark.parametrize("source", ["pair", "pair-repost"])
@pytest.mark.parametrize("include_evidence", [False, True])
def test_retired_pair_marker_cannot_pass_eligibility(published_native, source, include_evidence):
    result, rendered, _ = published_native
    lines = rendered.splitlines()
    lines[0] = cli.review_marker(result.head, "primary", source)
    if not include_evidence:
        del lines[1]
    review = row(result, "\n".join(lines))
    assert eligible(review) is None


@pytest.mark.parametrize("kind", ["normal", "repost", "tampered_digest", "changed_raw", "delta", "delta_legacy",
                                  "verified_inspection"])
def test_large_publication_confirmation_and_hosted_agreement(native, gh, fake_bin, monkeypatch, kind):
    from tests.test_github import pr_json, rest_pr_json, threads_json

    result = native[0]()
    repo = "acme/widgets"
    cfg = Config(repo, "main", ("true",), (), "squash", ("codex",), review_publishers=("bot",))
    if kind.startswith("delta"):
        reviewed, merge_base = result.head, native[1]["base"]
        git(native[2], "config", "core.autocrlf", "false")
        content = b"hello\r\n" if kind == "delta" else b"caf\xe9\r\n"
        (native[2] / "README.md").write_bytes(content)
        git(native[2], "commit", "-qam", "delta repair")
        head = git(native[2], "rev-parse", "HEAD").strip()
        git(native[2], "update-ref", "refs/remotes/origin/main", merge_base)
        (native[2] / ".loopzero").mkdir()
        (native[2] / ".loopzero/task.md").write_text("Inspect the delta")
        tool = fake_bin / "codex"
        old_range, new_range = f"{merge_base!r},{reviewed!r}", f"{reviewed!r},{head!r}"
        tool.write_text(tool.read_text().replace(old_range, new_range))
        result = cli._run_review(native[2], cfg, head, "delta", reviewed,
                                 number=7, publisher="bot")
        proof = runners.codex_evidence(result)
        assert proof["kind"] == "delta" and proof["calls"][0]["base"] == reviewed != merge_base
        argv = ["git", "diff", "--no-color", reviewed, head]
        delta_diff = subprocess.check_output(argv, cwd=native[2], env=git_environment())
        assert b"-hello\n+" + content in delta_diff
        if kind == "delta_legacy":
            assert "+caf\\xe9\r\n" in runners.render_patch(delta_diff)
        assert proof["calls"][0]["inspection"] == "unknown"
        assert proof["calls"][0]["diff_sha256"] == hashlib.sha256(delta_diff).hexdigest()
    head = result.head
    prefix = (
        cli._repost_prefix(result) if kind == "repost" else cli.review_marker(head, result.kind)
    )
    gh.respond(f"api repos/{repo}/pulls/7/files", [])
    rendered = github.review_payload(repo, 7, head, result, prefix)["body"]
    review = row(result, rendered)
    if kind == "normal":
        expected_comment = {"path": "large.txt", "line": 1, "side": "RIGHT", "body": "Finding body"}
        listed_comment = dict(expected_comment, line=None, side=None, commit_id=head,
                              pull_request_review_id=100, user={"login": "bot"})
        monkeypatch.setattr(github, "review_payloads", lambda *_a, **_k: [
            {"body": rendered, "comments": [expected_comment]},
        ])
    if kind == "tampered_digest":
        line = rendered.splitlines()[1]
        value = runners.decode_codex_evidence(line)
        value["raw_sha256"] = "f" * 64
        review["body"] = rendered.replace(line, packed(value))
    if kind == "changed_raw":
        result = dataclasses.replace(result, raw=json.dumps(json.loads(result.raw), indent=2))
    if kind == "verified_inspection":  # published before inspection was retired
        line = rendered.splitlines()[1]
        review["body"] = rendered.replace(line, packed(legacy_inspection(runners.decode_codex_evidence(line))))
    gh.respond(f"api repos/{repo}/pulls/7",
               rest_pr_json(pr_json(headRefOid=head, mergeStateStatus="CLEAN")))
    gh.respond(f"api repos/{repo}/pulls/7/reviews --method GET", [review])
    gh.respond(f"api repos/{repo}/pulls/7/reviews/100/comments --method GET", [listed_comment] if kind == "normal" else [])
    gh.respond("api graphql", threads_json())
    gitdir = native[2] / ".git/info/exclude"
    gitdir.write_text(gitdir.read_text() + "\n.loopzero/\n")
    cli._save_review(native[2], result)
    cli._save_report(
        native[2], CheckReport(head, False, (CheckResult("true", 0, 0, ""),),
                               cli.sandbox.settings_fingerprint(cfg))
    )
    pr = github.pr_view(repo, 7)
    assert (
        cli._readiness(native[2], cfg, pr, head, [review]).ready
        is eligibility.evaluate(repo, 7, head, "main", ("bot",)).ready
    )
    rejected = kind in ("tampered_digest", "changed_raw")
    with pytest.raises(cli.CliError, match="conflicting or ambiguous") if rejected else nullcontext():
        assert cli._confirm_review(native[2], cfg, pr, result, "bot", [review])
        if kind == "normal":
            for field, value in (("path", "other.txt"), ("body", "Different body"),
                                 ("commit_id", "different-commit"), ("line", 2), ("side", "LEFT")):
                monkeypatch.setattr(cli.github, "paged", lambda *_args, f=field, v=value, **_kw: [{**listed_comment, f: v}])
                with pytest.raises(cli.CliError, match="review finding contents or locations differ"):
                    cli._confirm_review(native[2], cfg, pr, result, "bot", [review])


@pytest.mark.parametrize("location", ["provenance", "model_prose", "model_fence"])
def test_saved_or_model_supplied_summary_cannot_grant_inspection(published_native, location):
    result, _, forged = published_native
    data = json.loads(result.raw)
    data["events"] = [
        e for e in data["events"] if e.get("item", {}).get("type") != "command_execution"
    ]
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
    assert projection(changed)[1]["calls"][0]["inspection"] == "unknown"


@pytest.mark.parametrize("field,value", [
    ("raw_bytes", True), ("raw_bytes", 0), ("raw_bytes", 2**31), ("chunk_count", 0),
    ("repo", "x" * 257), ("publisher", "/private"), ("kind", []), ("complete", 1),
])
def test_strict_projection_types_and_bounds(field, value):
    evidence = runners.codex_evidence(SYNTHETIC_NATIVE)
    evidence[field] = value
    assert not runners.valid_codex_evidence(evidence)
    with pytest.raises((ValueError, runners.RunnerBadOutput)):
        runners.decode_codex_evidence(packed(evidence))


def test_non_object_publisher_is_ignored_not_a_crash():
    review = row(SYNTHETIC_NATIVE)
    assert cli._pr_markers([review], review["commit_id"], ("bot",), repo="acme/widgets", number=7)
    review["user"] = "bot"
    assert cli._pr_markers([review], review["commit_id"], ("bot",), repo="acme/widgets", number=7) == []


@pytest.mark.parametrize("rendered, publication, diagnostic", [
    (cli.review_marker(SYNTHETIC_NATIVE.head, "primary"), ("acme/widgets", 8), "publication context mismatch"),
    (cli.review_marker(SYNTHETIC_NATIVE.head, "delta"), ("acme/widgets", 7), "invalid/oversized leading machine block"),
    (cli.review_marker("b" * 40, "primary"), ("acme/widgets", 7), "invalid/oversized leading machine block"),
])
def test_codex_publication_rejects_inconsistent_context(rendered, publication, diagnostic):
    with pytest.raises(runners.RunnerBadOutput, match=diagnostic):
        github._review_body(SYNTHETIC_NATIVE, [], rendered, publication=publication)


@pytest.mark.parametrize("header, diagnostic", [
    ("é", "invalid evidence header"),
    ("a" * (runners.CODEX_HEADER_LIMIT + 1), "invalid evidence header"),
    ("not a review marker", "invalid evidence encoding"),
    (packed_json('{"version":1,"version":2}'), "duplicate JSON key"),
])
def test_codex_evidence_rejects_invalid_transport(header, diagnostic):
    with pytest.raises(ValueError, match=diagnostic):
        runners.decode_codex_evidence(header)


def test_native_evidence_projection_requires_schema_validation(monkeypatch):
    monkeypatch.setattr(runners, "valid_codex_evidence", lambda _evidence: False)
    with pytest.raises(runners.RunnerBadOutput, match="invalid full native evidence"):
        runners._codex_evidence(SYNTHETIC_NATIVE)


def test_legacy_toolfree_remains_unknown_without_projection(git_repo):
    raw = json.loads(SYNTHETIC_NATIVE.raw)
    raw.pop("review_context")
    legacy = dataclasses.replace(SYNTHETIC_NATIVE, raw=json.dumps(raw))
    assert runners.validate_runner_result(legacy) and runners.codex_evidence(legacy) is None
    review = row(legacy)
    assert runners.CODEX_HEADER not in review["body"] and eligible(review)
    cli._save_review(git_repo, legacy)
    assert cli._load_review(git_repo, legacy.head, "primary")[0].raw == legacy.raw


def test_legacy_truncated_transcript_remains_unknown():
    head = "a" * 40
    rendered = (cli.review_marker(head, "primary")
                + f"\n{review_summary(head, family='codex')}\n<details><summary>Raw reviewer output</summary>"
                + "\n\n```\n[Transcript truncated] not JSON\n```\n</details>")
    legacy = row(SYNTHETIC_NATIVE, rendered) | {"id": 1}
    assert eligibility.review_projection(legacy, head, "acme/widgets", 7) == (False, None)
    assert eligible(legacy) is legacy


@pytest.mark.parametrize("identity", ["another-session", None, {}])
def test_contradictory_native_session_fields(native, identity):
    def change(data):
        data["events"][2].update(thread_id=data["session_id"], session_id=identity)

    assert_invalid_native(rewrite(native[0](), change))


@pytest.mark.parametrize("separator,overflow,tightened", [
    *((separator, overflow, False)
      for separator in ("\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
      for overflow in (0, 1, 2)),
    pytest.param("\n", 1, True, id="adjudication-counts-toward-tightened-budget"),
])
def test_raw_leading_utf8_span_at_limit_and_overflow(separator, overflow, tightened, monkeypatch):
    result = SYNTHETIC_NATIVE
    marker = cli.review_marker(result.head, result.kind)
    header = runners.encode_codex_evidence(runners.codex_evidence(result))
    adjudication = "<!-- loopzero:adjudication v=1 replaces=1 target=" + result.head + " -->"
    span = separator.join((marker, header, adjudication))
    padded = marker
    if tightened:
        monkeypatch.setattr(runners, "CODEX_HEADER_LIMIT", len((marker + "\n" + header).encode()))
    else:
        padded = marker[:-3] + " " * (8192 + overflow - len(span.encode("utf-8"))) + marker[-3:]
        span = separator.join((padded, header, adjudication))
        assert len(span.encode("utf-8")) == 8192 + overflow
    rendered = span + separator + review_summary(result.head, result.kind, family="codex")
    present, evidence = projection(result, rendered)
    assert present and (evidence is not None) is (overflow == 0)
    if not tightened:
        assert (eligible(row(result), row(result, rendered)) is not None) is (overflow == 0)
    if separator == "\n":  # Publication stays LF; readers count every accepted separator verbatim.
        if overflow:
            with pytest.raises(runners.RunnerBadOutput, match=r"leading machine block.*split the PR"):
                github._review_body(result, [], padded + "\n" + adjudication)
        else:
            assert github._review_body(result, [], padded + "\n" + adjudication).startswith(padded)


def test_codex_overflow_names_the_recovery(monkeypatch):
    evidence = runners.codex_evidence(SYNTHETIC_NATIVE)
    monkeypatch.setattr(runners, "CODEX_HEADER_LIMIT", 100)
    with pytest.raises(runners.RunnerBadOutput, match=r"evidence header.*split the PR"):
        runners.encode_codex_evidence(evidence)


@pytest.mark.parametrize("case", ["v1", "extended", "v2", "extra", "bad-settings", "too-long", "untrusted"])
def test_hosted_eligibility_replays_synthetic_codex_v1(monkeypatch, case):
    review, ready = json.loads((Path(__file__).parent / "fixtures/codex-v1-hosted-synthetic.json").read_text())[case]
    head, repo, number = review["commit_id"], "acme/widgets", 7
    pr = github.PR(number, "https://example.invalid", head, "main", False, "OPEN", "MERGEABLE")
    monkeypatch.setattr(github, "pr_view", lambda *_: pr)
    monkeypatch.setattr(github, "paged", lambda *_: [review])
    monkeypatch.setattr(github, "open_blocking_findings", lambda *_: [])
    assert eligibility.evaluate(repo, number, head, "main", ("bot",)).ready is ready
