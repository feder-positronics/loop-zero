"""Independent-review regressions. All native/process/network operations are fake."""

import dataclasses
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from loopzero import _proc, cli, eligibility, github, native_pair, runners
from tests.test_native_pair import BASE, GRANT, HEAD, configuration, native_for_call

DIFF = "diff --git a/fake b/fake\n+fake"


@pytest.fixture(autouse=True)
def deny_external(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("No real subprocess/network/native execution")

    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def capture_for_call(kw, *, task=None):
    result = native_for_call(kw)
    raw = json.loads(result.raw)
    prompt = runners.build_prompt(
        kind=kw["kind"],
        head=kw["head"],
        task_text=kw["task_text"] if task is None else task,
        diff=kw["diff"],
    )
    raw["review_context"]["prompt_sha256"] = native_pair.digest(prompt)
    return dataclasses.replace(result, raw=json.dumps(raw))


def invoke_pair(cwd, *, task="ACTUAL TASK"):
    return native_pair.run(
        cwd,
        configuration(),
        HEAD,
        "primary",
        DIFF,
        task,
        ("acme/widgets", 7, "bot"),
        GRANT,
        "",
        None,
        BASE,
    )


def review_inputs(monkeypatch):
    monkeypatch.setattr(cli, "_primary_base", lambda *a: BASE)
    monkeypatch.setattr(cli.worktree, "diff_since", lambda *a: DIFF)
    monkeypatch.setattr(cli.worktree, "task_text", lambda *a: "ACTUAL TASK")


def invoke_cli(cwd):
    return cli._run_review(
        cwd,
        configuration(),
        HEAD,
        "primary",
        None,
        publication=("acme/widgets", 7, "bot"),
        pair_grant=GRANT,
    )


def test_wrong_first_task_same_head_diff_stops_before_astra(monkeypatch, tmp_path):
    calls = []

    def fake(family, **kw):
        calls.append(kw["model"])
        return capture_for_call(kw, task="ANOTHER COMMISSIONED TASK")

    monkeypatch.setattr(runners, "review_with", fake)
    with pytest.raises(runners.RunnerBadOutput, match="caller"):
        invoke_pair(tmp_path)
    assert calls == [native_pair.MODELS[0]]


@pytest.mark.parametrize("boundary", ["private", "saved", "public"])
def test_different_member_prompts_cannot_survive_replay(monkeypatch, tmp_path, boundary):
    monkeypatch.setattr(runners, "review_with", lambda family, **kw: capture_for_call(kw))
    valid = invoke_pair(tmp_path)
    assert runners.validate_runner_result(valid)
    leaves = list(valid.members)
    raw = json.loads(leaves[1].raw)
    raw["review_context"]["prompt_sha256"] = native_pair.digest("OTHER TASK")
    leaves[1] = dataclasses.replace(leaves[1], raw=json.dumps(raw))
    changed = dataclasses.replace(
        valid,
        members=tuple(leaves),
        raw=native_pair.canonical(
            {"type": "codex_review_pair", "members": [m.raw for m in leaves]}
        ),
    )
    if boundary == "private":
        assert not runners.validate_runner_result(changed)
    elif boundary == "saved":
        cli._save_review(tmp_path, changed)
        with pytest.raises(cli.CliError, match="provenance/envelope"):
            cli._load_review(tmp_path, HEAD, "primary")
    else:
        try:
            body = github._review_body(
                changed,
                [],
                cli.review_marker(HEAD, "primary", "pair"),
                publication=("acme/widgets", 7),
            )
            row = {
                "id": 1,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "user": {"login": "bot"},
                "body": body,
            }
            accepted = eligibility.eligible_review(
                [row], HEAD, ("bot",), repo="acme/widgets", number=7
            )
        except runners.RunnerBadOutput:
            accepted = None
        assert accepted is None


def test_overlapping_inflight_attempt_refuses_before_native(monkeypatch, tmp_path):
    review_inputs(monkeypatch)
    calls = []

    def fake(family, **kw):
        calls.append(kw["model"])
        if len(calls) == 1:
            with pytest.raises(cli.CliError, match="attempt"):
                invoke_cli(tmp_path)
        kw["capture"](
            {"exit_code": 0, "stdout": "complete fake capture", "stderr": "", "duration_s": 0.1}
        )
        return capture_for_call(kw)

    monkeypatch.setattr(runners, "review_with", fake)
    result = invoke_cli(tmp_path)
    assert runners.validate_runner_result(result)
    assert calls == list(native_pair.MODELS)


def test_interrupted_attempt_cannot_dispatch_again(monkeypatch, tmp_path):
    review_inputs(monkeypatch)
    calls = []

    def interrupted(family, **kw):
        calls.append(kw["model"])
        raise KeyboardInterrupt("fake interruption after native admission")

    monkeypatch.setattr(runners, "review_with", interrupted)
    with pytest.raises(KeyboardInterrupt):
        invoke_cli(tmp_path)
    try:
        invoke_cli(tmp_path)
        second_error = None
    except (cli.CliError, KeyboardInterrupt) as exc:
        second_error = exc
    assert isinstance(second_error, cli.CliError)
    assert "attempt" in str(second_error)
    assert calls == [native_pair.MODELS[0]]
    state = json.loads(next((tmp_path / ".loopzero").glob("*-pair-attempt.json")).read_text())
    assert state["state"] == "interrupted"


@pytest.mark.parametrize(
    "stdout,stderr", [(b"FULL STDOUT", b"PRIVATE STDERR AUTH DETAIL"), ("out", "err"), (None, None)]
)
def test_actual_timeout_conversion_preserves_both_streams_and_elapsed(
    monkeypatch, tmp_path, stdout, stderr
):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("FAKE", 1, output=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", timeout)
    times = iter([100.0, 102.5])
    monkeypatch.setattr(_proc.time, "monotonic", lambda: next(times))
    captured = []
    with pytest.raises(runners.RunnerBadOutput, match="timed out"):
        runners._run(
            ["FAKE-NOT-EXECUTED"],
            cwd=tmp_path,
            prompt="fake",
            env_allowlist=(),
            timeout=1,
            family="codex",
            capture=captured.append,
        )
    expected_out = stdout.decode() if isinstance(stdout, bytes) else stdout or ""
    expected_err = stderr.decode() if isinstance(stderr, bytes) else stderr or ""
    assert captured == [
        {
            "exit_code": None,
            "stdout": expected_out,
            "stderr": expected_err,
            "duration_s": 2.5,
            "timeout": True,
        }
    ]


def test_historical_recovery_explicitly_rejects_v2_shape():
    assert (
        eligibility.recovery_inspection({"version": 2, "head": HEAD, "complete": True}, HEAD)
        is False
    )


def test_two_concurrent_callers_acquire_only_one_native_attempt(monkeypatch, tmp_path):
    review_inputs(monkeypatch)
    start = Barrier(2)
    calls = []

    def fake(family, **kw):
        calls.append(kw["model"])
        kw["capture"](
            {
                "exit_code": 71,
                "stdout": "FULL FAILED OUTPUT",
                "stderr": "PRIVATE AUTH DETAIL",
                "duration_s": 0.25,
            }
        )
        raise runners.RunnerAuthFailed("fake native auth error")

    monkeypatch.setattr(runners, "review_with", fake)

    def invoke():
        start.wait(timeout=3)
        try:
            invoke_cli(tmp_path)
        except (cli.CliError, runners.RunnerAuthFailed) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke) for _ in range(2)]
        outcomes = [future.result(timeout=5) for future in futures]
    assert sorted(outcomes) == ["CliError", "RunnerAuthFailed"]
    assert calls == [native_pair.MODELS[0]]
    state = json.loads(next((tmp_path / ".loopzero").glob("*-pair-attempt.json")).read_text())
    assert state["state"] == "failed" and state["prompt_sha256"]
    raw = json.loads(next((tmp_path / ".loopzero").glob("*-pair-1-native.json")).read_text())
    assert raw["stdout"] == "FULL FAILED OUTPUT" and raw["stderr"] == "PRIVATE AUTH DETAIL"


def test_empty_incomplete_reservation_still_refuses_without_reacquire(monkeypatch, tmp_path):
    review_inputs(monkeypatch)
    directory = tmp_path / ".loopzero"
    directory.mkdir()
    attempt = directory / f"review-{HEAD[:12]}-primary-pair-attempt.json"
    attempt.write_bytes(b"")  # process stopped immediately after atomic creation
    monkeypatch.setattr(runners, "review_with", lambda *a, **kw: pytest.fail("no native dispatch"))
    with pytest.raises(cli.CliError, match="attempt"):
        invoke_cli(tmp_path)
    assert attempt.read_bytes() == b""
    assert not list(directory.glob("*-native.json"))


def test_invalid_input_does_not_reserve_an_attempt(monkeypatch, tmp_path):
    review_inputs(monkeypatch)
    monkeypatch.setattr(runners, "review_with", lambda *a, **kw: pytest.fail("no native dispatch"))
    with pytest.raises(runners.RunnerBadOutput, match="exact operator"):
        cli._run_review(
            tmp_path,
            configuration(),
            HEAD,
            "primary",
            None,
            publication=("acme/widgets", 7, "bot"),
            pair_grant=GRANT + "0",
        )
    assert not (tmp_path / ".loopzero").exists()


def test_actual_native_exit124_is_not_a_supervisor_timeout(monkeypatch, tmp_path):
    def completed(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 124, stdout="received output", stderr="native stderr"
        )

    monkeypatch.setattr(subprocess, "run", completed)
    times = iter([10.0, 11.5])
    monkeypatch.setattr(_proc.time, "monotonic", lambda: next(times))
    captured = []
    with pytest.raises(runners.RunnerBadOutput, match="exited 124"):
        runners._run(
            ["FAKE-NOT-EXECUTED"],
            cwd=tmp_path,
            prompt="fake",
            env_allowlist=(),
            timeout=1,
            family="codex",
            capture=captured.append,
        )
    assert captured == [
        {
            "exit_code": 124,
            "stdout": "received output",
            "stderr": "native stderr",
            "duration_s": 1.5,
        }
    ]


def test_proc_timeout_legacy_output_alias_keeps_unknown_duration():
    exc = _proc.ProcTimeout("legacy", "stdout")
    assert exc.output == exc.stdout == "stdout"
    assert exc.stderr == "" and exc.duration_s is None


@pytest.mark.parametrize("mutation", ["missing", "unknown", "member", "root"])
def test_public_prompt_commitments_are_required_and_shared(monkeypatch, tmp_path, mutation):
    monkeypatch.setattr(runners, "review_with", lambda family, **kw: capture_for_call(kw))
    proof = runners.codex_evidence(invoke_pair(tmp_path))
    assert native_pair.valid_public(proof)
    if mutation == "missing":
        proof["members"][0].pop("prompt_sha256")
    if mutation == "unknown":
        proof["prompt_sha256"] = None
    if mutation == "member":
        proof["members"][1]["prompt_sha256"] = "f" * 64
    if mutation == "root":
        proof["prompt_sha256"] = "f" * 64
    proof["binding_sha256"] = native_pair.digest(
        native_pair.canonical({k: v for k, v in proof.items() if k != "binding_sha256"})
    )
    assert not native_pair.valid_public(proof)


def test_two_changed_prompts_cannot_replace_the_saved_original(monkeypatch, tmp_path):
    monkeypatch.setattr(runners, "review_with", lambda family, **kw: capture_for_call(kw))
    result = invoke_pair(tmp_path)
    leaves = []
    for member in result.members:
        raw = json.loads(member.raw)
        raw["review_context"]["prompt_sha256"] = "f" * 64
        leaves.append(dataclasses.replace(member, raw=json.dumps(raw)))
    changed = dataclasses.replace(
        result,
        members=tuple(leaves),
        raw=native_pair.canonical(
            {"type": "codex_review_pair", "members": [m.raw for m in leaves]}
        ),
    )
    assert not runners.validate_runner_result(changed)


def test_attempt_receipt_is_retained_by_cleanup(monkeypatch, tmp_path, capsys):
    directory = tmp_path / ".loopzero"
    directory.mkdir()
    (directory / "review-fake-pair-attempt.json").write_text(
        json.dumps({"native_pair_attempt": True, "state": "started"})
    )
    cli._cleanup(tmp_path)
    assert "retaining native review receipt" in capsys.readouterr().out
    assert directory.exists()
