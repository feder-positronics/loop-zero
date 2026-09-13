import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


from loopzero.review import harness as module


@pytest.fixture(autouse=True)
def legacy_unscoped_finding_compatibility(monkeypatch):
    """The source harness predates the package's required PR composition seam."""
    from loopzero.review import findings

    monkeypatch.setattr(findings, "_REQUIRE_PR_SCOPE", False)


def _structured_advisory(*findings: dict[str, object]) -> str:
    return json.dumps({"findings": list(findings), "limitations": []})


def _write_events(tmp_path: Path, events: list[dict]) -> None:
    import json

    audit = tmp_path / ".audit" / "agent-events"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "2026-08-03.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )


def test_prior_terminal_attempt_blocks_only_real_attempts(
    tmp_path: Path,
) -> None:
    """One attempt per logical gate (#3106 B2): pass/fail and timeout/budget
    skips are terminal; availability skips and old events are not."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat(timespec="seconds")
    base = {"kind": "tool", "category": "cross-harness-review", "ts": now}

    _write_events(tmp_path, [{**base, "scope_digest": "d1", "result": "pass"}])
    assert module._prior_terminal_attempt(tmp_path, scope_digest="d1") == "pass"
    assert module._prior_terminal_attempt(tmp_path, scope_digest="other") is None

    _write_events(
        tmp_path,
        [
            {
                **base,
                "scope_digest": "d2",
                "result": "skip",
                "notes": "claude CLI unavailable",
            },
            {
                **base,
                "scope_digest": "d3",
                "result": "skip",
                "notes": "Claude timed out after 120s",
            },
            {
                **base,
                "scope_digest": "d4",
                "result": "pass",
                "ts": "2026-07-01T00:00:00Z",
            },
        ],
    )
    assert module._prior_terminal_attempt(tmp_path, scope_digest="d2") is None
    assert module._prior_terminal_attempt(tmp_path, scope_digest="d3") == "skip"
    assert module._prior_terminal_attempt(tmp_path, scope_digest="d4") is None


def test_structured_advisory_findings_are_classified_for_the_ledger() -> None:
    finding = {
        "severity": "important",
        "claim": "Unsafe config crosses the trust boundary.",
        "path": "app/config.py",
        "line_start": 4,
        "line_end": 4,
    }
    parsed = module.parse_advisory_result(_structured_advisory(finding))

    assert parsed.findings == (finding,)
    assert parsed.finding_count == 1
    assert parsed.max_severity == "important"


def test_chain_advisory_appends_child_receipt_without_section_authority(
    monkeypatch, tmp_path: Path
) -> None:
    from loopzero.review.findings import build_finding_capture_request

    primary = {
        "schema_version": "ReviewChainReceiptV1",
        "chain_id": "rc_" + "a" * 32,
        "task_id": "primary-review",
        "snapshot_sha": "b" * 40,
        "snapshot_tree_sha": "c" * 40,
        "patch_identity": {"schema_version": "PatchIdentityV1"},
        "required_sections": ["code", "security"],
        "sections": {
            "code": {"completion": "completed"},
            "security": {"completion": "completed"},
        },
    }
    terminal = {
        "task_id": "primary-review",
        "review_chain_receipt": primary,
        "source_identity": {"head": "d" * 40},
    }
    finding = {
        "severity": "important",
        "claim": "Advisory boundary defect.",
        "path": "app/config.py",
        "line_start": 8,
        "line_end": 8,
    }
    appended: list[dict[str, object]] = []
    monkeypatch.setattr(module, "primary_repo_root", lambda _repo: tmp_path)

    def append_findings(*args, **kwargs):
        del args
        request = build_finding_capture_request(
            producer_kind=kwargs["producer_kind"],
            producer_id=kwargs["task_id"],
            unit_attempt_number=kwargs["unit_attempt_number"],
            producer_skill=kwargs["producer_skill"],
            category=kwargs["category"],
            advisory=kwargs["advisory"],
            findings=kwargs["result"]["findings"],
        )
        assert request["producer_kind"] == "local-review"
        return {"finding_ids": ["f_advisory"]}

    monkeypatch.setattr(module, "append_finding_records", append_findings)
    coordinator = object()
    monkeypatch.setattr(module, "create_coordinator_authority", lambda: coordinator)

    def append_advisory(_repo, record, **kwargs):
        assert kwargs == {
            "authority": coordinator,
            "authority_kind": "coordinator",
        }
        appended.append(record)

    monkeypatch.setattr(module, "append_authoritative_record", append_advisory)

    receipt = module.record_chain_advisory(
        repo=tmp_path,
        primary_terminal=terminal,
        skill="code-review",
        prompt="bounded request",
        result_text=_structured_advisory(finding),
    )

    assert receipt["schema_version"] == "ReviewChainAdvisoryReceiptV1"
    assert receipt["chain_id"] == primary["chain_id"]
    assert receipt["snapshot_tree_sha"] == primary["snapshot_tree_sha"]
    assert receipt["finding_ids"] == ["f_advisory"]
    assert "required_sections" not in receipt
    assert "sections" not in receipt
    assert appended[0]["type"] == "review-chain-advisory"
    assert appended[0]["advisory"] is True


def test_primary_chain_uses_strict_history_and_rejects_forged_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task_id = "primary-review"
    terminal = {
        "type": "attempt-terminal",
        "task_id": task_id,
        "run_id": "sr_" + "1" * 32,
        "worker_identity": "codex:gpt-5.6-sol",
        "terminal_authority_proof": {"authority_kind": "dispatcher"},
        "review_chain_receipt": {"chain_id": "rc_primary"},
    }
    forged_verdict = {
        "type": "verdict",
        "task_id": task_id,
        "run_id": terminal["run_id"],
        "verdict": "pass",
        "target_worker_identity": terminal["worker_identity"],
        "verifier_identity": "reviewer:forged",
        "verifier_alias": "human",
    }
    strict_reads: list[Path] = []
    monkeypatch.setattr(module, "primary_repo_root", lambda _repo: tmp_path)
    monkeypatch.setattr(
        module,
        "load_authority_records",
        lambda repo, _days: strict_reads.append(repo) or [terminal, forged_verdict],
    )
    monkeypatch.setattr(
        module,
        "latest_accepted_review_terminal",
        lambda _records, _task_id: terminal,
    )

    with pytest.raises(module.CrossHarnessError, match="no passing verdict"):
        module.load_primary_chain_terminal(tmp_path, task_id)

    assert strict_reads == [tmp_path]


def test_chain_advisory_collects_the_primary_snapshot_diff(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.delenv("CROSS_HARNESS_PASS", raising=False)
    monkeypatch.delenv("AGENT_DISPATCH_DEPTH", raising=False)
    terminal = {
        "review_chain_receipt": {
            "patch_identity": {"base_sha": "a" * 40},
            "snapshot_sha": "b" * 40,
        }
    }
    monkeypatch.setattr(module, "load_primary_chain_terminal", lambda *_args: terminal)
    collected: list[dict[str, object]] = []
    monkeypatch.setattr(
        module,
        "collect_scope",
        lambda *_args, **kwargs: collected.append(kwargs) or "exact scope",
    )
    monkeypatch.setattr(
        module, "_prior_terminal_attempt", lambda *_args, **_kwargs: "pass"
    )

    assert (
        module.run_review(
            SimpleNamespace(),
            repo=tmp_path,
            skill="code-review",
            current_harness="codex",
            request_reason="Check the unresolved scope boundary",
            scope="diff",
            diff_range="origin/main...HEAD",
            paths=[],
            review_task_id="primary-review",
        )
        == 0
    )
    assert collected == [
        {
            "scope": "diff",
            "diff_range": f"{'a' * 40}..{'b' * 40}",
            "paths": [],
        }
    ]
    assert "one attempt per logical gate" in capsys.readouterr().err


def test_run_review_refuses_second_attempt_for_same_gate(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.delenv("CROSS_HARNESS_PASS", raising=False)
    monkeypatch.delenv("AGENT_DISPATCH_DEPTH", raising=False)
    monkeypatch.setattr(module, "collect_scope", lambda *a, **k: "scope text")
    monkeypatch.setattr(
        module, "_prior_terminal_attempt", lambda repo, scope_digest: "pass"
    )
    launched: list[str] = []
    monkeypatch.setattr(module, "review_prompt", lambda *a: launched.append("x") or "p")

    result = module.run_review(
        SimpleNamespace(),
        repo=tmp_path,
        skill="code-review",
        current_harness="codex",
        request_reason="Check the unresolved scope boundary",
        scope="diff",
        diff_range="HEAD",
        paths=[],
    )

    assert result == 0
    assert launched == []
    assert "one attempt per logical gate" in capsys.readouterr().err

    # --new-gate proceeds past the guard (fails later on prompt/runner,
    # proving the guard was the only stop).
    monkeypatch.setattr(module, "CLAUDE_MAX_PROMPT_CHARS", 0)
    skipped: list[dict] = []
    monkeypatch.setattr(
        module, "_skip", lambda _repo, **kwargs: skipped.append(kwargs) or 0
    )
    module.run_review(
        SimpleNamespace(),
        repo=tmp_path,
        skill="code-review",
        current_harness="codex",
        request_reason="Check the unresolved scope boundary",
        scope="diff",
        diff_range="HEAD",
        paths=[],
        new_gate=True,
    )
    assert launched == ["x"]
    assert skipped[0]["reason"].startswith("scope too large")


def test_claude_packaging_limits_match_locked_decision() -> None:
    assert module.CLAUDE_TIMEOUT_SECONDS == 120
    assert module.DIFF_CONTEXT_LINES == 10
    assert module.LONG_DIFF_LINE_CHARS == 20_000
    assert module.CLAUDE_MAX_PROMPT_CHARS == 225_000


@pytest.mark.skip(reason="(c) native Claude command construction moved to runners.claude")
def test_claude_command_is_bounded_and_tool_free() -> None:
    assert module.claude_command() == [
        "timeout",
        "120s",
        "claude",
        "-p",
        "--safe-mode",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--model",
        "opus",
        "--effort",
        "low",
        "--max-budget-usd",
        "1.00",
        "--output-format",
        "json",
    ]


@pytest.mark.skip(reason="(c) native Codex invocation moved to runners.codex")
def test_codex_working_tree_review_uses_uncommitted_mode() -> None:
    command, input_text = module.codex_invocation("diff", "HEAD")

    assert command == [
        "timeout",
        "600s",
        "codex",
        "review",
        "--uncommitted",
        "-",
    ]
    assert input_text == "prompt"


@pytest.mark.skip(reason="(c) native Codex invocation moved to runners.codex")
def test_codex_branch_review_receives_trailer_prompt() -> None:
    command, input_text = module.codex_invocation("diff", "origin/main...HEAD")

    assert command == [
        "timeout",
        "600s",
        "codex",
        "review",
        "--base",
        "main",
        "-",
    ]
    assert input_text == "prompt"


def test_collect_diff_scope_includes_stat_and_exact_diff(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class _Runner:
        def run(self, command, *, input_text=None):
            del input_text
            calls.append(command)
            if command[:3] == ["git", "ls-files", "--others"]:
                output = ""
            elif "--name-only" in command:
                output = "app.py\0"
            else:
                output = "stat-output" if "--stat" in command else "diff-output"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    scope = module.collect_scope(
        _Runner(),
        tmp_path,
        scope="diff",
        diff_range="origin/main...HEAD",
        paths=[],
    )

    assert "stat-output" in scope
    assert "diff-output" in scope
    assert calls == [
        ["git", "diff", "--stat", "origin/main...HEAD"],
        [
            "git",
            "diff",
            "--name-only",
            "-z",
            "origin/main...HEAD",
        ],
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--unified=10",
            "origin/main...HEAD",
            "--",
            "app.py",
        ],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]


def test_collect_diff_scope_rejects_empty_diff(tmp_path: Path) -> None:
    class _Runner:
        def run(self, command, *, input_text=None):
            del command, input_text
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        module.collect_scope(
            _Runner(),
            tmp_path,
            scope="diff",
            diff_range="origin/main...HEAD",
            paths=[],
        )
    except module.CrossHarnessError as exc:
        assert "diff is empty" in str(exc)
    else:
        raise AssertionError("empty diffs should not buy an alternate review")


def test_collect_diff_scope_includes_untracked_files(tmp_path: Path) -> None:
    (tmp_path / "new.py").write_text("print('new')\n", encoding="utf-8")

    class _Runner:
        def run(self, command, *, input_text=None):
            del input_text
            output = (
                "new.py\n" if command[:3] == ["git", "ls-files", "--others"] else ""
            )
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    scope = module.collect_scope(
        _Runner(),
        tmp_path,
        scope="diff",
        diff_range="HEAD",
        paths=[],
    )

    assert "===== new.py =====" in scope
    assert "print('new')" in scope


def test_collect_diff_scope_compacts_pathological_generated_line(
    tmp_path: Path,
) -> None:
    long_unchanged_line = " " + ("ExistingType," * 2_000)

    class _Runner:
        def run(self, command, *, input_text=None):
            del input_text
            if "--stat" in command:
                output = "generated.ts | 2 +-\n"
            elif "--name-only" in command:
                output = "generated.ts\0"
            elif "--word-diff=porcelain" in command:
                output = (
                    "diff --git a/generated.ts b/generated.ts\n"
                    "index old..new 100644\n"
                    "--- a/generated.ts\n"
                    "+++ b/generated.ts\n"
                    "@@ -1 +1 @@\n"
                    f"{long_unchanged_line}\n"
                    "+LlmUsageOperationRowResponse,\n"
                    "~\n"
                )
            elif command[:3] == ["git", "ls-files", "--others"]:
                output = ""
            else:
                output = (
                    "diff --git a/generated.ts b/generated.ts\n"
                    f"-{long_unchanged_line}\n"
                    f"+{long_unchanged_line}LlmUsageOperationRowResponse,\n"
                )
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    scope = module.collect_scope(
        _Runner(),
        tmp_path,
        scope="diff",
        diff_range="HEAD",
        paths=[],
    )

    assert "COMPACTED LONG-LINE DIFF: generated.ts" in scope
    assert "+LlmUsageOperationRowResponse," in scope
    assert long_unchanged_line not in scope


@pytest.mark.skip(reason="(c) runner adapter owns native Claude launch admission")
def test_oversized_claude_prompt_skips_before_launch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    skipped: list[dict] = []

    class _Runner:
        def run(self, command, *, input_text=None):
            del input_text
            calls.append(command)
            if command and command[0] == "timeout":
                raise AssertionError("oversized prompt must not launch Claude")
            if "--stat" in command:
                output = "app.py | 1 +\n"
            elif "--name-only" in command:
                output = "app.py\0"
            elif command[:3] == ["git", "ls-files", "--others"]:
                output = ""
            else:
                output = "diff --git a/app.py b/app.py\n+" + ("x" * 200)
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    def fake_skip(_repo, **kwargs):
        skipped.append(kwargs)
        return 0

    monkeypatch.setattr(module, "CLAUDE_MAX_PROMPT_CHARS", 100)
    monkeypatch.setattr(module, "_skip", fake_skip)

    result = module.run_review(
        _Runner(),
        repo=tmp_path,
        skill="code-review",
        current_harness="codex",
        request_reason="Check the unresolved scope boundary",
        scope="diff",
        diff_range="HEAD",
        paths=[],
    )

    assert result == 0
    assert len(skipped) == 1
    assert skipped[0]["reason"].startswith("scope too large for Claude (")
    assert skipped[0]["reason"].endswith(" chars > 100 cap)")
    assert skipped[0]["prompt_chars"] > 100
    assert skipped[0]["timeout_seconds"] == 120
    assert all(command[0] != "timeout" for command in calls)


@pytest.mark.skip(reason="(c) event persistence moved to loopzero.kernel.events")
def test_emit_event_records_prompt_size_and_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._emit_event(
        tmp_path,
        skill="code-review",
        duration_ms=42_000,
        result="pass",
        provider="claude",
        scope_digest="digest",
        prompt_chars=52_890,
        timeout_seconds=120,
    )

    assert len(commands) == 1
    assert "--prompt-chars" in commands[0]
    assert commands[0][commands[0].index("--prompt-chars") + 1] == "52890"
    assert "--timeout-seconds" in commands[0]
    assert commands[0][commands[0].index("--timeout-seconds") + 1] == "120"


@pytest.mark.skip(reason="(c) normalized runner contracts replace native Claude argv")
def test_successful_claude_review_records_prompt_size_and_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[dict] = []
    prompts: list[str] = []

    class _Runner:
        def run(self, command, *, input_text=None):
            if command and command[0] == "timeout":
                assert input_text is not None
                prompts.append(input_text)
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        '{"result":"No findings","total_cost_usd":0.03,'
                        '"is_error":false}'
                    ),
                    stderr="",
                )
            if "--stat" in command:
                output = "app.py | 1 +\n"
            elif "--name-only" in command:
                output = "app.py\0"
            elif command[:3] == ["git", "ls-files", "--others"]:
                output = ""
            else:
                output = "diff --git a/app.py b/app.py\n+safe_change = True\n"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(module.shutil, "which", lambda _binary: "/usr/bin/claude")
    monkeypatch.setattr(
        module,
        "_emit_event",
        lambda _repo, **kwargs: events.append(kwargs),
    )

    result = module.run_review(
        _Runner(),
        repo=tmp_path,
        skill="code-review",
        current_harness="codex",
        request_reason="Check the unresolved scope boundary",
        scope="diff",
        diff_range="HEAD",
        paths=[],
    )

    assert result == 0
    assert len(prompts) == 1
    assert events == [
        {
            "skill": "code-review",
            "duration_ms": events[0]["duration_ms"],
            "result": "pass",
            "notes": "request: Check the unresolved scope boundary",
            "provider": "claude",
            "scope_digest": events[0]["scope_digest"],
            "cost_usd": 0.03,
            "budget_usd": 1.0,
            "prompt_chars": len(prompts[0]),
            "timeout_seconds": 120,
            # The stub reply is "No findings", so the advisory pass recorded a
            # clean residual-miss observation rather than an unknown one.
            "finding_count": 0,
            "max_severity": "none",
        }
    ]


def test_claude_failure_reason_is_sanitized() -> None:
    assert (
        module._claude_failure_reason(
            1,
            module.ClaudeResult(
                text="Execution stopped: max budget exceeded",
                cost_usd=1.2,
                is_error=True,
            ),
        )
        == "Claude budget exhausted"
    )
    assert (
        module._claude_failure_reason(
            1,
            module.ClaudeResult(
                text="Prompt exceeds the context window",
                cost_usd=None,
                is_error=True,
            ),
        )
        == "Claude context limit"
    )
    assert (
        module._claude_failure_reason(
            1,
            module.ClaudeResult(
                text="Rate limit reached; retry later",
                cost_usd=None,
                is_error=True,
            ),
        )
        == "Claude rate limited"
    )
    assert (
        module._claude_failure_reason(
            1,
            module.ClaudeResult(
                text="raw provider detail that must not escape",
                cost_usd=None,
                is_error=True,
            ),
        )
        == "Claude returned an error"
    )


def test_collect_file_scope_rejects_path_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")

    try:
        module.collect_scope(
            object(),
            tmp_path,
            scope="files",
            diff_range="origin/main...HEAD",
            paths=[str(outside)],
        )
    except module.CrossHarnessError as exc:
        assert "outside the repository" in str(exc)
    else:
        raise AssertionError("scope collection must reject paths outside the repo")


def test_collect_file_scope_rejects_gitignored_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    class _Runner:
        def run(self, command, *, input_text=None):
            del command, input_text
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        module.collect_scope(
            _Runner(),
            tmp_path,
            scope="files",
            diff_range="HEAD",
            paths=[".env"],
        )
    except module.CrossHarnessError as exc:
        assert "ignored or unavailable" in str(exc)
    else:
        raise AssertionError("ignored files must never be sent to another harness")


def test_parse_claude_result_extracts_text_cost_and_error() -> None:
    result = module.parse_claude_result(
        '{"result":"No findings","total_cost_usd":0.37,"is_error":false}'
    )

    assert result.text == "No findings"
    assert result.cost_usd == 0.37
    assert result.is_error is False


def test_recursion_guard_skips_without_spawning(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CROSS_HARNESS_PASS", "1")
    calls: list[list[str]] = []

    class _Runner:
        def run(self, command, *, input_text=None):
            calls.append(command)
            raise AssertionError("recursion guard must not spawn another harness")

    assert (
        module.run_review(
            _Runner(),
            repo=tmp_path,
            skill="code-review",
            current_harness="codex",
            request_reason="Check the unresolved scope boundary",
            scope="diff",
            diff_range="origin/main...HEAD",
            paths=[],
        )
        == 0
    )
    assert calls == []


@pytest.mark.skip(reason="(c) model selection is validated by routing and runner tests")
def test_no_vetoed_model_is_routed() -> None:
    """Owner veto 2026-07-26: Sonnet is not used anywhere in this repo.

    The exact-command assertion above already pins the model, but it reads as an
    arbitrary string. This states the actual invariant, so the next person to
    change the model sees why one value is excluded rather than merely different.
    """
    assert "sonnet" not in module.claude_command()


@pytest.mark.skip(reason="(a/c) CLI controller removed; direct registry guard has target coverage")
@pytest.mark.parametrize("reason", [None, "", " \t\n"])
@pytest.mark.parametrize("entry", ["cli", "runner"])
def test_default_skips_before_any_review_work(
    monkeypatch, tmp_path, capsys, reason, entry
):
    def forbidden(*args, **kwargs):
        pytest.fail("default invocation performed review work")

    for name in (
        "collect_scope",
        "load_primary_chain_terminal",
        "detect_current_harness",
        "_prior_terminal_attempt",
        "Runner",
        "_emit_event",
    ):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.shutil, "which", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    if entry == "cli":
        args = ["--skill", "security-review", "--review-task-id", "risky-large-primary"]
        if reason is not None:
            args += ["--request-reason", reason]
        result = module.main(args)
    else:
        kwargs = {} if reason is None else {"request_reason": reason}
        result = module.run_review(
            SimpleNamespace(run=forbidden),
            repo=tmp_path,
            skill="security-review",
            current_harness="auto",
            scope="diff",
            diff_range="HEAD",
            paths=[],
            review_task_id="risky-large-primary",
            new_gate=True,
            **kwargs,
        )
    assert result == 0
    assert (
        "cross-harness pass skipped (not explicitly requested)"
        in capsys.readouterr().err
    )


@pytest.mark.skip(reason="(a) consumer CLI front controller stays outside loopzero")
def test_cli_passes_explicit_request_to_runner(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(module, "detect_current_harness", lambda: "codex")
    monkeypatch.setattr(
        module, "run_review", lambda *args, **kwargs: calls.append(kwargs) or 0
    )
    assert (
        module.main(
            [
                "--skill",
                "review",
                "--request-reason",
                "  Check boundary  ",
                "--repo",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert calls[0]["request_reason"] == "Check boundary"


@pytest.mark.skip(reason="(c) normalized runner-result tests replace provider argv assertions")
@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("returncode", [0, 1, 124])
def test_explicit_request_preserves_provider_outcome_and_telemetry(
    monkeypatch, tmp_path, harness, returncode
):
    monkeypatch.delenv("CROSS_HARNESS_PASS", raising=False)
    monkeypatch.delenv("AGENT_DISPATCH_DEPTH", raising=False)
    monkeypatch.setattr(module, "collect_scope", lambda *a, **k: "exact scope")
    monkeypatch.setattr(module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    events = []
    calls = []
    monkeypatch.setattr(module, "_emit_event", lambda repo, **kw: events.append(kw))
    result_text = _structured_advisory()
    stdout = (
        json.dumps({"result": result_text, "is_error": False})
        if harness == "codex"
        else result_text
    )

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    assert (
        module.run_review(
            SimpleNamespace(run=run),
            repo=tmp_path,
            skill="review",
            current_harness=harness,
            scope="files",
            diff_range="HEAD",
            paths=["app.py"],
            request_reason="  Check the unresolved boundary  ",
        )
        == 0
    )
    assert len(calls) == 1
    assert len(events) == 1
    assert events[0]["result"] == ("pass" if returncode == 0 else "skip")
    assert "request: Check the unresolved boundary" in events[0]["notes"]


def test_request_reason_does_not_turn_availability_skip_into_terminal_attempt(tmp_path):
    from datetime import UTC, datetime

    _write_events(
        tmp_path,
        [
            {
                "kind": "tool",
                "category": "cross-harness-review",
                "scope_digest": "scope",
                "ts": datetime.now(UTC).isoformat(),
                "result": "skip",
                "notes": "claude CLI unavailable; request: Check the budget after the primary timed out",
            }
        ],
    )
    assert module._prior_terminal_attempt(tmp_path, scope_digest="scope") is None


def test_explicit_request_preserves_dispatched_worker_guard(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("CROSS_HARNESS_PASS", raising=False)
    monkeypatch.setenv("AGENT_DISPATCH_DEPTH", "1")
    monkeypatch.setattr(
        module, "collect_scope", lambda *a, **k: pytest.fail("worker collected scope")
    )
    assert (
        module.run_review(
            SimpleNamespace(),
            repo=tmp_path,
            skill="review",
            current_harness="codex",
            scope="diff",
            diff_range="HEAD",
            paths=[],
            request_reason="Check boundary",
        )
        == 0
    )
    assert "dispatched worker" in capsys.readouterr().err


@pytest.mark.skip(reason="(c) runner timeout and one-attempt behavior has registry coverage")
@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_opted_in_timeout_is_terminal_for_the_same_scope(
    monkeypatch, tmp_path, capsys, harness
):
    from datetime import UTC, datetime

    monkeypatch.delenv("CROSS_HARNESS_PASS", raising=False)
    monkeypatch.delenv("AGENT_DISPATCH_DEPTH", raising=False)
    monkeypatch.setattr(module, "collect_scope", lambda *a, **k: "exact scope")
    monkeypatch.setattr(module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    events = []
    launches = []

    def emit(repo, **event):
        events.append(
            {
                **event,
                "kind": "tool",
                "category": "cross-harness-review",
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        _write_events(repo, events)

    def run(command, **kwargs):
        launches.append(command)
        return SimpleNamespace(returncode=124, stdout="", stderr="")

    monkeypatch.setattr(module, "_emit_event", emit)
    kwargs = dict(
        repo=tmp_path,
        skill="review",
        current_harness=harness,
        scope="files",
        diff_range="HEAD",
        paths=["app.py"],
        request_reason="Check the unresolved boundary",
    )
    runner = SimpleNamespace(run=run)
    assert module.run_review(runner, **kwargs) == 0
    assert events[0]["notes"].startswith("timeout; request: ")
    assert module.run_review(runner, **kwargs) == 0
    assert len(launches) == 1
    assert "one attempt per logical gate" in capsys.readouterr().err
