"""Fake native execution only; no provider/model/real GitHub commands."""

import copy
import dataclasses
import json

import pytest

from loopzero import cli, eligibility, github, native_pair, runners
from loopzero.types import Config, ReviewConfig
from tests.test_runners import _native_codex_result

HEAD = "a" * 40
BASE = "b" * 40
GRANT = f"acme/widgets#7@{HEAD}"


def configuration(reviewers=("codex",)):
    return Config(
        "acme/widgets",
        "main",
        (),
        (),
        "squash",
        reviewers,
        review={"codex": ReviewConfig("ignored-config-model", "medium", ("medium",))},
    )


def member(model):
    result = _native_codex_result(HEAD, session="fake-" + model)
    raw = json.loads(result.raw)
    raw["review_context"]["requested"]["model"] = model
    raw["reported_settings"]["model"] = model
    return dataclasses.replace(
        result,
        raw=json.dumps(raw),
        model=model,
        provenance={
            **result.provenance,
            "requested_settings": [raw["review_context"]["requested"]],
        },
    )


def pair():
    return native_pair.compose(
        [member(m) for m in native_pair.MODELS], GRANT, prompt_sha256="1" * 64
    )


def published(result):
    body = github._review_body(
        result, [], cli.review_marker(HEAD, "primary", "pair"), publication=("acme/widgets", 7)
    )
    return {
        "id": 1,
        "commit_id": HEAD,
        "user": {"login": "bot"},
        "state": "COMMENTED",
        "body": body,
    }


def eligible(rows):
    return eligibility.eligible_review(rows, HEAD, ("bot",), repo="acme/widgets", number=7)


def test_native_pair_two_full_captures_public_private_and_saved(tmp_path):
    result = pair()
    assert runners.validate_runner_result(result)
    assert all(json.loads(r.raw)["review_context"]["chunks"] == 1 for r in result.members)
    public = published(result)
    assert "codex-evidence v=2" in public["body"].splitlines()[1]
    assert eligible([public]) == public
    assert all(
        secret not in public["body"] for secret in ("/synthetic/repo", "git -C", "corrected")
    )
    cli._save_review(tmp_path, result)
    loaded = cli._load_review(tmp_path, HEAD, "primary")
    assert loaded == result
    assert published(loaded)["body"] == public["body"]
    assert (tmp_path / ".loopzero").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "same_model",
        "session",
        "head",
        "range",
        "publisher",
        "raw",
        "union",
        "grant",
        "model_unknown",
        "fatal",
        "terminal",
    ],
)
def test_native_pair_rejects_incomplete_tampered_or_wrong_scope(change):
    result = pair()
    if change == "missing":
        result = dataclasses.replace(result, members=result.members[:1])
    elif change == "same_model":
        result = dataclasses.replace(result, members=(result.members[0], result.members[0]))
    elif change == "raw":
        result = dataclasses.replace(result, raw=result.raw + " ")
    elif change == "union":
        result = dataclasses.replace(result, verdict="request_changes")
    elif change == "grant":
        result = dataclasses.replace(result, pair_authority=GRANT + "0")
    else:
        leaves = list(result.members)
        raw = json.loads(leaves[1].raw)
        if change == "session":
            raw["session_id"] = "duplicate"
        if change == "head":
            raw["review_context"]["head"] = "c" * 40
        if change == "range":
            raw["review_context"]["base"] = "c" * 40
        if change == "publisher":
            raw["review_context"]["publisher"] = "other"
        if change == "model_unknown":
            raw["reported_settings"]["model"] = None
        if change == "terminal":
            raw["events"].pop()
        if change == "fatal":
            raw["events"].insert(
                1,
                {
                    "type": "item.completed",
                    "item": {
                        "type": "error",
                        "message": "Code Mode is unavailable because failed to spawn code-mode host /x/codex-code-mode-host: host executable was not found. Code mode will fail closed;",
                    },
                },
            )
        leaves[1] = dataclasses.replace(leaves[1], raw=json.dumps(raw))
        result = dataclasses.replace(result, members=tuple(leaves))
    assert not runners.validate_runner_result(result)
    with pytest.raises((runners.RunnerBadOutput, ValueError)):
        published(result)


def test_no_first_success_native_order_or_model_substitution(monkeypatch, tmp_path):
    config = configuration(("claude", "codex"))
    calls = []

    def fake(family, **kw):
        calls.append((family, kw))
        return native_for_call(kw)

    monkeypatch.setattr(runners, "review_with", fake)
    result = native_pair.run(
        tmp_path,
        config,
        HEAD,
        "primary",
        "diff --git a/fake b/fake\n+fake",
        "task",
        ("acme/widgets", 7, "bot"),
        GRANT,
        "",
        None,
        BASE,
    )
    assert result.members and [kw["model"] for _, kw in calls] == list(native_pair.MODELS)
    assert all(family == "codex" and "chunk" not in kw for family, kw in calls)
    assert all(
        kw["review_context"]["base"] == BASE and kw["diff"] == "diff --git a/fake b/fake\n+fake"
        for _, kw in calls
    )
    calls.clear()

    def fail(family, **kw):
        calls.append(kw["model"])
        raise runners.RunnerUsageLimit("codex", "fake quota")

    monkeypatch.setattr(runners, "review_with", fail)
    with pytest.raises(runners.RunnerUsageLimit):
        native_pair.run(
            tmp_path,
            config,
            HEAD,
            "primary",
            "diff",
            "task",
            ("acme/widgets", 7, "bot"),
            GRANT,
            "",
            None,
            BASE,
        )
    assert calls == [native_pair.MODELS[0]]


@pytest.mark.parametrize(
    "change",
    [
        "missing_header",
        "missing_member",
        "same_model",
        "bad_hash",
        "wrong_publisher",
        "unreconciled",
        "wrong_marker",
    ],
)
def test_shared_consumer_invalid_present_never_revives_older(change):
    good = published(pair())
    bad = copy.deepcopy(good)
    if change == "missing_header":
        bad["body"] = bad["body"].splitlines()[0] + "\n**approve**"
    elif change == "wrong_publisher":
        bad["user"]["login"] = "attacker"
    elif change == "wrong_marker":
        bad["body"] = bad["body"].replace("source=pair", "source=model")
    else:
        proof = runners.codex_evidence(pair())
        if change == "missing_member":
            proof["members"].pop()
        if change == "same_model":
            proof["members"][1] = copy.deepcopy(proof["members"][0])
        if change == "bad_hash":
            proof["raw_sha256"] = "invalid"
        if change == "unreconciled":
            proof["disagreements"] = True
        import base64

        header = (
            native_pair.HEADER
            + base64.urlsafe_b64encode(native_pair.canonical(proof).encode()).decode().rstrip("=")
            + " -->"
        )
        lines = bad["body"].splitlines()
        lines[1] = header
        bad["body"] = "\n".join(lines)
    if change == "wrong_publisher":
        # An untrusted publication does not revoke trusted evidence under the ordinary publisher contract.
        assert eligible([bad]) is None
    else:
        assert eligible([good, bad]) is None


def test_pair_remains_one_primary_budget():
    with pytest.raises(cli.CliError, match="already has a primary"):
        cli._decide_kind([cli.Marker(HEAD, "primary", "COMMENTED")], HEAD)


def test_pair_runner_rejects_a_capture_for_another_full_input(monkeypatch, tmp_path):
    config = configuration()
    monkeypatch.setattr(runners, "review_with", lambda family, **kw: member(kw["model"]))
    with pytest.raises(runners.RunnerBadOutput, match="caller"):
        native_pair.run(
            tmp_path,
            config,
            HEAD,
            "primary",
            "different actual full input",
            "task",
            ("acme/widgets", 7, "bot"),
            GRANT,
            "",
            None,
            BASE,
        )


def differing_pair():
    sol, astra = [member(m) for m in native_pair.MODELS]
    raw = json.loads(astra.raw)
    payload = {
        "verdict": "request_changes",
        "findings": [
            {
                "severity": "important",
                "path": "feature.py",
                "line": 1,
                "title": "Actual fake disagreement",
                "body": "Must remain blocking",
            }
        ],
    }
    raw["final_message"] = json.dumps(payload)
    raw["events"][-2]["item"]["text"] = raw["final_message"]
    parsed = runners.parse_review(
        payload, family="codex", head=HEAD, kind="primary", raw=json.dumps(raw)
    )
    astra = dataclasses.replace(
        astra, verdict=parsed.verdict, findings=parsed.findings, raw=json.dumps(raw)
    )
    return native_pair.compose([sol, astra], GRANT, prompt_sha256="1" * 64)


def test_reconciliation_retains_union_and_never_turns_rc_into_approval(tmp_path):
    result = differing_pair()
    assert result.verdict == "request_changes" and len(result.findings) == 1
    with pytest.raises(runners.RunnerBadOutput, match="explicit reconciliation"):
        published(result)
    cli._save_review(tmp_path, result)
    saved = cli._load_review(tmp_path, HEAD, "primary")
    reconciled = native_pair.compose(
        saved.members,
        GRANT,
        "Inspected both reports; retain complete union and RC",
        prompt_sha256="1" * 64,
    )
    row = published(reconciled)
    assert eligible([row]) is None
    assert (
        eligibility.eligible_review(
            [row], HEAD, ("bot",), repo="acme/widgets", number=7, publication_only=True
        )
        == row
    )
    assert reconciled.findings == result.findings and reconciled.verdict == "request_changes"
    assert runners.validate_runner_result(reconciled)
    assert json.loads(reconciled.members[0].raw) == json.loads(result.members[0].raw)


def test_native_error_even_with_successful_tool_is_not_pair_evidence():
    result = pair()
    leaves = list(result.members)
    raw = json.loads(leaves[1].raw)
    raw["events"].insert(
        1,
        {
            "type": "item.completed",
            "item": {"type": "error", "message": "native error, not a quoted agent message"},
        },
    )
    leaves[1] = dataclasses.replace(leaves[1], raw=json.dumps(raw))
    with pytest.raises(runners.RunnerBadOutput, match="native error"):
        native_pair.compose(leaves, GRANT, prompt_sha256="1" * 64)


def test_ordinary_author_family_default_and_config_do_not_authorize_pair(monkeypatch, tmp_path):
    config = configuration()
    monkeypatch.setattr(cli, "_primary_base", lambda *a: BASE)
    monkeypatch.setattr(cli.worktree, "diff_since", lambda *a: "diff --git a/a b/a\n+test")
    monkeypatch.setattr(cli.worktree, "task_text", lambda *a: "task")
    monkeypatch.setattr(runners, "author_families", lambda *a: {"codex"})
    monkeypatch.setattr(
        runners, "review_with", lambda *a, **kw: pytest.fail("must not run a model")
    )
    with pytest.raises(cli.IndependentReviewerUnavailable):
        cli._run_review(tmp_path, config, HEAD, "primary", None, model="gpt-6.1-sol")


def test_pair_private_capture_retention_including_nonzero_and_timeout(monkeypatch, tmp_path):
    from loopzero import _proc

    captured = []

    class Done:
        exit_code = 1
        stdout = "FULL PRIVATE STDOUT " + "x" * 40000
        stderr = "reached your usage limit"
        duration_s = 1

    monkeypatch.setattr(_proc, "run", lambda *a, **kw: Done())
    with pytest.raises(runners.RunnerUsageLimit):
        runners._run(
            ["fake-codex"],
            cwd=tmp_path,
            prompt="fake",
            env_allowlist=(),
            timeout=1,
            family="codex",
            capture=captured.append,
        )
    assert captured[0]["stdout"] == Done.stdout and captured[0]["stderr"] == Done.stderr
    assert captured[0]["exit_code"] == 1


def test_pair_projection_checksums_and_public_bounds():
    proof = runners.codex_evidence(pair())
    assert native_pair.valid_public(proof)
    proof["members"][0]["evidence"]["raw_sha256"] = "f" * 64
    assert not native_pair.valid_public(proof)
    with pytest.raises(runners.RunnerBadOutput):
        runners.encode_codex_evidence(proof)


def test_pair_cleanup_retains_private_transport(tmp_path, capsys):
    directory = tmp_path / ".loopzero"
    directory.mkdir()
    (directory / "review-fake-pair-1-native.json").write_text(
        json.dumps({"native_pair_transport": True, "stdout": "private"})
    )
    cli._cleanup(tmp_path)
    assert "retaining native review receipt" in capsys.readouterr().out
    assert directory.exists()


@pytest.mark.parametrize(
    "token", [None, "wrong/widgets#7@" + HEAD, "acme/widgets#8@" + HEAD, GRANT + "0"]
)
def test_pair_operator_context_required_before_any_native_call(monkeypatch, tmp_path, token):
    config = configuration()
    monkeypatch.setattr(
        runners, "review_with", lambda *a, **kw: pytest.fail("must hold before model")
    )
    with pytest.raises(runners.RunnerBadOutput, match="exact operator"):
        native_pair.run(
            tmp_path,
            config,
            HEAD,
            "primary",
            "fake",
            "task",
            ("acme/widgets", 7, "bot"),
            token,
            "",
            None,
            BASE,
        )


def test_present_pair_marker_in_wrong_position_is_not_legacy():
    good = published(pair())
    bad = copy.deepcopy(good)
    bad["body"] = (
        "human text before machine marker\n" + bad["body"].splitlines()[0] + "\n**approve**"
    )
    assert eligible([good, bad]) is None


def test_publication_header_overflow_keeps_both_raws_private(tmp_path):
    from loopzero.types import Finding

    leaves = []
    for model in native_pair.MODELS:
        result = member(model)
        findings = tuple(
            Finding("suggestion", None, None, "synthetic " + str(i), "PRIVATE suggestion text")
            for i in range(40)
        )
        payload = {"verdict": "approve", "findings": [dataclasses.asdict(f) for f in findings]}
        raw = json.loads(result.raw)
        raw["final_message"] = json.dumps(payload)
        raw["events"][-2]["item"]["text"] = raw["final_message"]
        leaves.append(dataclasses.replace(result, findings=findings, raw=json.dumps(raw)))
    result = native_pair.compose(leaves, GRANT, prompt_sha256="1" * 64)
    cli._save_review(tmp_path, result)
    with pytest.raises(runners.RunnerBadOutput, match="8192"):
        published(result)
    assert cli._load_review(tmp_path, HEAD, "primary").raw == result.raw


def test_native_capture_timeout_keeps_full_received_output(monkeypatch, tmp_path):
    from loopzero import _proc

    output = "private timeout output " + "x" * 40000

    def timeout(*a, **kw):
        raise _proc.ProcTimeout("fake bounded timeout", output)

    monkeypatch.setattr(_proc, "run", timeout)
    captured = []
    with pytest.raises(runners.RunnerBadOutput, match="timed out"):
        runners._run(
            ["fake-codex"],
            cwd=tmp_path,
            prompt="fake",
            env_allowlist=(),
            timeout=1,
            family="codex",
            capture=captured.append,
        )
    assert captured == [
        {"exit_code": None, "stdout": output, "stderr": "", "duration_s": None, "timeout": True}
    ]


from tests import test_cli as cli_fixtures

repo, wt, gh = cli_fixtures.repo, cli_fixtures.wt, cli_fixtures.gh


def native_for_call(kw):
    context = kw["review_context"]
    result = _native_codex_result(
        kw["head"],
        base=context["base"],
        patch=kw["diff"],
        repo=context["repo"],
        number=context["pr"],
        publisher=context["publisher"],
        cwd=str(kw["cwd"].resolve()),
        session="fake-full-" + kw["model"],
    )
    raw = json.loads(result.raw)
    settings = {"model": kw["model"], "effort": kw["effort"]}
    raw["reported_settings"] = raw["review_context"]["requested"] = settings
    raw["review_context"]["fragment_sha256"] = native_pair.digest(kw["diff"])
    raw["review_context"]["prompt_sha256"] = native_pair.digest(
        runners.build_prompt(
            kind=kw["kind"], head=kw["head"], task_text=kw["task_text"], diff=kw["diff"]
        )
    )
    return dataclasses.replace(
        result,
        raw=json.dumps(raw),
        model=kw["model"],
        effort=kw["effort"],
        provenance={**result.provenance, "requested_settings": [settings]},
    )


def configure_pair_cli(monkeypatch):
    original = cli._review_launch_config

    def launch(args, wt, config):
        config, source = original(args, wt, config)
        return dataclasses.replace(
            config, review={"codex": ReviewConfig("ignored", "medium", ("medium",))}
        ), source

    monkeypatch.setattr(cli, "_review_launch_config", launch)
    monkeypatch.setattr(runners, "author_families", lambda *a: {"codex"})


def test_normal_cli_pair_posts_one_primary_and_confirms_repost(
    wt, gh, fake_bin, capsys, monkeypatch
):
    head = cli_fixtures.prepare_review(wt, gh, fake_bin)
    configure_pair_cli(monkeypatch)
    calls = []

    def fake(family, **kw):
        calls.append(kw["model"])
        kw["capture"](
            {"exit_code": 0, "stdout": "fake complete raw native transport", "stderr": ""}
        )
        return native_for_call(kw)

    monkeypatch.setattr(runners, "review_with", fake)
    grant = native_pair.authority("acme/widgets", 7, head)
    code, _, error = cli_fixtures.run(capsys, "review", "--sol-astra-pair", grant)
    assert (code, error) == (0, "")
    assert calls == list(native_pair.MODELS)
    payload = cli_fixtures.posted_review(gh)
    assert payload["body"].startswith(cli.review_marker(head, "primary", "pair") + "\n")
    assert "codex-evidence v=2" in payload["body"]
    saved = cli._load_review(wt, head, "primary")
    assert len(saved.members) == 2
    assert len(list((wt / ".loopzero").glob("*-pair-*-native.json"))) == 2
    reviews = [
        {
            "id": 1,
            "commit_id": head,
            "state": "COMMENTED",
            "user": {"login": cli_fixtures.LOGIN},
            "body": payload["body"],
        }
    ]
    gh.respond(cli_fixtures.reviews_key(), reviews)
    gh.respond("api repos/acme/widgets/pulls/7/reviews/1/comments?per_page=100&page=1", [])
    code, _, error = cli_fixtures.run(capsys, "review", "--repost", "--sol-astra-pair", grant)
    assert (code, error) == (0, "")
    assert calls == list(native_pair.MODELS)  # confirmation never reruns either model
    posts = [c for c in gh.calls if c["argv"][:2] == cli_fixtures.post_key().split()]
    assert len(posts) == 1


@pytest.mark.parametrize("failure_index", [1, 2])
def test_cli_pair_native_failure_stops_and_retains_actual_error_transport(
    wt, gh, fake_bin, capsys, monkeypatch, failure_index
):
    head = cli_fixtures.prepare_review(wt, gh, fake_bin)
    configure_pair_cli(monkeypatch)
    calls = []

    def fake(family, **kw):
        calls.append(kw["model"])
        if len(calls) < failure_index:
            kw["capture"]({"exit_code": 0, "stdout": "first complete fake capture", "stderr": ""})
            return native_for_call(kw)
        kw["capture"](
            {
                "exit_code": 71,
                "stdout": "fake native failure entire stdout",
                "stderr": "fake auth error",
            }
        )
        raise runners.RunnerAuthFailed("fake native auth failure")

    monkeypatch.setattr(runners, "review_with", fake)
    code, _, error = cli_fixtures.run(
        capsys, "review", "--sol-astra-pair", native_pair.authority("acme/widgets", 7, head)
    )
    assert code == 1 and "fake native auth failure" in error
    assert calls == list(native_pair.MODELS[:failure_index])
    assert not any(c["argv"][:2] == cli_fixtures.post_key().split() for c in gh.calls)
    capture = json.loads(
        next((wt / ".loopzero").glob(f"*-pair-{failure_index}-native.json")).read_text()
    )
    assert capture["exit_code"] == 71 and capture["stderr"] == "fake auth error"
    code, _, error = cli_fixtures.run(
        capsys, "review", "--sol-astra-pair", native_pair.authority("acme/widgets", 7, head)
    )
    assert code == 1 and "do not retry" in error
    assert len(calls) == failure_index


@pytest.mark.parametrize("case", ["valid", "request_changes", "invalid_latest"])
def test_local_and_hosted_shared_pair_predicate(wt, gh, case):
    from loopzero.types import CheckReport
    from tests.test_github import pr_json, rest_pr_json, threads_json

    result = (
        pair()
        if case != "request_changes"
        else native_pair.compose(
            differing_pair().members, GRANT, "Retain union", prompt_sha256="1" * 64
        )
    )
    row = published(result)
    reviews = [row]
    if case == "invalid_latest":
        reviews.append({**row, "id": 2, "body": row["body"].splitlines()[0] + "\n**approve**"})
    gh.respond(
        "api repos/acme/widgets/pulls/7",
        rest_pr_json(pr_json(headRefOid=HEAD, mergeStateStatus="CLEAN")),
    )
    gh.respond("api graphql", threads_json())
    gh.respond("api repos/acme/widgets/pulls/7/reviews?per_page=100&page=1", reviews)
    config = Config(
        "acme/widgets", "main", (), (), "squash", ("codex",), review_publishers=("bot",)
    )
    cli._save_report(wt, CheckReport(HEAD, False, ()))
    local = cli._readiness(wt, config, github.pr_view(config.repo, 7), HEAD, reviews)
    hosted = eligibility.evaluate(config.repo, 7, HEAD, "main", ("bot",))
    assert local.ready is hosted.ready is (case == "valid")
    assert local.reasons == hosted.reasons


def test_pair_saved_repost_prefix_remains_typed():
    result = pair()
    body = github._review_body(
        result, [], cli._repost_prefix(result), publication=("acme/widgets", 7)
    )
    row = published(result)
    row["body"] = body
    assert "source=pair-repost" in body.splitlines()[0]
    assert eligible([row]) == row


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "same_model",
        "session",
        "range",
        "publisher",
        "unknown_model",
        "native_error",
        "version_type",
        "pr_type",
        "unreconciled",
    ],
)
def test_pair_shared_schema_rejects_even_recomputed_metadata_checksum(mutation):
    proof = runners.codex_evidence(pair())
    if mutation == "missing":
        proof["members"].pop()
    elif mutation == "same_model":
        proof["members"][1] = copy.deepcopy(proof["members"][0])
    elif mutation == "session":
        proof["members"][1]["evidence"]["calls"][0]["session"] = proof["members"][0]["evidence"][
            "calls"
        ][0]["session"]
    elif mutation == "range":
        proof["members"][1]["evidence"]["calls"][0]["base"] = "c" * 40
    elif mutation == "publisher":
        proof["members"][1]["evidence"]["publisher"] = "other"
    elif mutation == "unknown_model":
        proof["members"][1]["evidence"]["calls"][0]["reported"]["model"] = None
    elif mutation == "native_error":
        proof["members"][1]["native_errors"] = 1
    elif mutation == "version_type":
        proof["version"] = 2.0
    elif mutation == "pr_type":
        proof["pr"] = 7.0
        proof["authority"] = native_pair.authority("acme/widgets", 7.0, HEAD)
    elif mutation == "unreconciled":
        proof["members"][1]["verdict"] = "request_changes"
    proof["binding_sha256"] = native_pair.digest(
        native_pair.canonical({k: v for k, v in proof.items() if k != "binding_sha256"})
    )
    assert not native_pair.valid_public(proof)


def test_duplicate_but_individually_valid_native_sessions_hold():
    sol, astra = [member(m) for m in native_pair.MODELS]
    session = sol.provenance["session_id"]
    raw = json.loads(astra.raw)
    raw["session_id"] = session
    raw["events"][0]["thread_id"] = session
    astra = dataclasses.replace(
        astra,
        raw=json.dumps(raw),
        provenance={**astra.provenance, "session_id": session, "session_ids": [session]},
    )
    assert runners.validate_runner_result(astra)
    with pytest.raises(runners.RunnerBadOutput, match="session/range"):
        native_pair.compose([sol, astra], GRANT, prompt_sha256="1" * 64)


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
@pytest.mark.parametrize("overflow", [0, 1])
def test_pair_original_utf8_machine_span_at_limit(separator, overflow):
    result = pair()
    marker = cli.review_marker(HEAD, "primary", "pair")
    header = runners.encode_codex_evidence(runners.codex_evidence(result))
    padding = 8192 + overflow - len((marker + separator + header).encode())
    assert padding > 0
    padded = marker[:-3] + " " * padding + marker[-3:]
    row = published(result)
    row["body"] = padded + separator + header + separator + "**approve**"
    assert len(runners.leading_review_block(row["body"])[1].encode()) == 8192 + overflow
    assert (eligible([row]) is not None) is (overflow == 0)


def test_reconciliation_explicitly_lists_each_models_unique_finding_ids():
    result = differing_pair()
    reconciled = native_pair.compose(
        result.members, GRANT, "Retain all findings from both native models", prompt_sha256="1" * 64
    )
    proof = runners.codex_evidence(reconciled)
    assert proof["differences"][native_pair.MODELS[0]] == []
    assert proof["differences"][native_pair.MODELS[1]] == native_pair.finding_ids(
        reconciled.members[1]
    )
    assert proof["union"] == proof["differences"][native_pair.MODELS[1]]


@pytest.mark.parametrize("failure", ["unknown_model", "native_error"])
def test_first_native_evidence_gap_stops_before_second_model(monkeypatch, tmp_path, failure):
    calls = []

    def fake(family, **kw):
        calls.append(kw["model"])
        result = native_for_call(kw)
        raw = json.loads(result.raw)
        if failure == "unknown_model":
            raw["reported_settings"]["model"] = None
        else:
            raw["events"].insert(
                1,
                {"type": "item.completed", "item": {"type": "error", "message": "native failure"}},
            )
        return dataclasses.replace(result, raw=json.dumps(raw))

    monkeypatch.setattr(runners, "review_with", fake)
    with pytest.raises(runners.RunnerBadOutput):
        native_pair.run(
            tmp_path,
            configuration(),
            HEAD,
            "primary",
            "diff --git a/fake b/fake\n+fake",
            "task",
            ("acme/widgets", 7, "bot"),
            GRANT,
            "",
            None,
            BASE,
        )
    assert calls == [native_pair.MODELS[0]]
