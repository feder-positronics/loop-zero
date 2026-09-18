"""Tests for loopzero.runners using fake ``claude``/``codex`` executables on PATH."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from loopzero.runners import (
    RunnerAuthFailed,
    RunnerBadOutput,
    RunnerMissing,
    author_family,
    build_prompt,
    pick_reviewer,
    review_with,
)

from .conftest import git

APPROVE = {"verdict": "approve", "findings": []}
CHANGES = {
    "verdict": "approve",  # deliberately wrong; runner must escalate
    "findings": [
        {"severity": "important", "path": "a.py", "line": 3, "title": "Bug", "body": "Off by one."},
        {"severity": "suggestion", "path": None, "line": None, "title": "Nit", "body": "Rename."},
    ],
}

def _argv(bin_dir: Path, name: str) -> list[str]:
    return (bin_dir / f"{name}.argv").read_text().split("\0")[:-1]


def _script(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


def _fake_claude(bin_dir: Path, stdout: object, *, exit_code: int = 0) -> Path:
    """Fake ``claude`` that checks argv shape, dumps argv and prints ``stdout``."""
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return _script(
        bin_dir,
        "claude",
        f"""
        printf '%s\\0' "$@" > "{bin_dir}/claude.argv"
        cat > "{bin_dir}/claude.stdin"
        case " $* " in
          *" -p "*) ;; *) echo "missing -p" >&2; exit 64;;
        esac
        case " $* " in
          *" --output-format json "*) ;; *) echo "missing --output-format json" >&2; exit 64;;
        esac
        case " $* " in
          *" --permission-mode plan "*) ;; *) echo "missing --permission-mode plan" >&2; exit 64;;
        esac
        case " $* " in
          *" --json-schema "*) ;; *) echo "missing --json-schema" >&2; exit 64;;
        esac
        cat <<'JSON'
        {text}
        JSON
        exit {exit_code}
        """,
    )


def _fake_codex(bin_dir: Path, last_message: str, *, exit_code: int = 0, stderr: str = "") -> Path:
    """Fake ``codex`` that checks argv shape and writes ``last_message`` to -o file."""
    return _script(
        bin_dir,
        "codex",
        f"""
        printf '%s\\0' "$@" > "{bin_dir}/codex.argv"
        printf '%s' "$CODEX_HOME" > "{bin_dir}/codex.home"
        [ -e "$CODEX_HOME/config.toml" ] && echo yes > "{bin_dir}/codex.config" || true
        [ ! -f "$CODEX_HOME/auth.json" ] || cat "$CODEX_HOME/auth.json" > "{bin_dir}/codex.auth"
        cat > "{bin_dir}/codex.stdin"
        [ "$1" = exec ] || {{ echo "expected exec subcommand" >&2; exit 64; }}
        out=""; schema=""
        while [ $# -gt 0 ]; do
          case "$1" in
            --output-last-message) out="$2"; shift;;
            --output-schema) schema="$2"; shift;;
            --sandbox) [ "$2" = read-only ] || {{ echo "bad sandbox $2" >&2; exit 64; }}; shift;;
          esac
          shift
        done
        [ -n "$out" ] || {{ echo "missing -o" >&2; exit 64; }}
        [ -f "$schema" ] || {{ echo "missing schema file" >&2; exit 64; }}
        [ -n "{stderr}" ] && echo "{stderr}" >&2
        cat > "$out" <<'MSG'
        {last_message}
        MSG
        exit {exit_code}
        """,
    )


def _claude_envelope(payload: object, **extra: object) -> dict[str, object]:
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": json.dumps(payload), "structured_output": payload, **extra}


def _review(family: str, cwd: Path, diff: str = "--- a\n+++ b\n+x\n"):
    return review_with(family, cwd=cwd, head="abc123", kind="primary",
                       diff=diff, task_text="Do the thing")


# ----------------------------------------------------------------- prompt


def test_prompt_contains_task_diff_and_json_contract() -> None:
    prompt = build_prompt(kind="delta", head="deadbeef", task_text="Objective", diff="+line")
    assert "Objective" in prompt and "+line" in prompt and "deadbeef" in prompt
    assert '"verdict": "approve"|"request_changes"' in prompt
    assert "critical" in prompt and "important" in prompt and "suggestion" in prompt
    assert "delta review" in prompt


# ----------------------------------------------------------------- claude


def test_claude_approve(fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    result = _review("claude", tmp_path)
    assert result.verdict == "approve" and result.findings == ()
    assert result.family == "claude" and result.head == "abc123" and result.kind == "primary"
    assert json.loads(result.raw)["structured_output"] == APPROVE
    argv = _argv(fake_bin, "claude")
    stdin = (fake_bin / "claude.stdin").read_text()
    assert stdin.startswith("You are an independent code reviewer")
    assert "Do the thing" in stdin and "+x" in stdin
    assert not any("Do the thing" in a for a in argv)
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["verdict"]["enum"] == ["approve", "request_changes"]
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--disallowedTools") + 1] == (
        "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"
    )
    assert "sk-secret" not in result.raw


def test_claude_request_changes_escalates_verdict(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, _claude_envelope(CHANGES))
    result = _review("claude", tmp_path)
    assert result.verdict == "request_changes"
    assert [f.severity for f in result.findings] == ["important", "suggestion"]
    assert result.findings[0].path == "a.py" and result.findings[0].line == 3
    assert result.findings[1].path is None and result.findings[1].line is None


def test_claude_falls_back_to_result_text(fake_bin: Path, tmp_path: Path) -> None:
    env = {"type": "result", "subtype": "success", "result": "```json\n" + json.dumps(APPROVE) + "\n```"}
    _fake_claude(fake_bin, env)
    assert _review("claude", tmp_path).verdict == "approve"


def test_claude_malformed_json(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, "this is not json at all")
    with pytest.raises(RunnerBadOutput) as info:
        _review("claude", tmp_path)
    assert "not json" in info.value.tail.lower()


def test_claude_bad_severity(fake_bin: Path, tmp_path: Path) -> None:
    bad = {"verdict": "approve", "findings": [{"severity": "nit", "path": None, "line": None,
                                               "title": "t", "body": "b"}]}
    _fake_claude(fake_bin, _claude_envelope(bad))
    with pytest.raises(RunnerBadOutput, match="bad severity"):
        _review("claude", tmp_path)


def test_claude_auth_failure_text(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, "Not logged in. Please run /login", exit_code=1)
    with pytest.raises(RunnerAuthFailed):
        _review("claude", tmp_path)


def test_claude_auth_failure_in_error_envelope(fake_bin: Path, tmp_path: Path) -> None:
    env = {"type": "result", "subtype": "error", "is_error": True,
           "result": "Invalid API key · Please run /login"}
    _fake_claude(fake_bin, env)
    with pytest.raises(RunnerAuthFailed):
        _review("claude", tmp_path)


AUTH_WORDS = {
    "verdict": "approve",
    "findings": [{"severity": "suggestion", "path": "auth.py", "line": 7,
                  "title": "Handle 401 Unauthorized", "body": "Log 'Not logged in' clearly."}],
}


def test_claude_success_with_auth_words_is_not_auth_failure(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, _claude_envelope(AUTH_WORDS))
    result = _review("claude", tmp_path)
    assert result.verdict == "approve" and result.findings[0].title == "Handle 401 Unauthorized"


def test_codex_success_with_auth_words_is_not_auth_failure(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, json.dumps(AUTH_WORDS), stderr="warning: not authenticated to telemetry")
    assert _review("codex", tmp_path).verdict == "approve"


def test_claude_nonzero_exit_is_bad_output(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, "boom", exit_code=2)
    with pytest.raises(RunnerBadOutput, match="exited 2"):
        _review("claude", tmp_path)


def test_claude_missing_binary(fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))  # nothing else on PATH
    with pytest.raises(RunnerMissing):
        _review("claude", tmp_path)


def test_env_is_stripped_to_allowlist_plus_auth(fake_bin: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _script(fake_bin, "claude", f'env > "{fake_bin}/env"; echo \'{json.dumps(_claude_envelope(APPROVE))}\'')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "other")
    monkeypatch.setenv("SOME_RANDOM_SECRET", "leak")
    _review("claude", tmp_path)
    env = (fake_bin / "env").read_text()
    assert "ANTHROPIC_API_KEY=sk-secret" in env
    assert "OPENAI_API_KEY" not in env and "SOME_RANDOM_SECRET" not in env


# ----------------------------------------------------------------- codex


def test_codex_approve(fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "configured-codex"
    configured.mkdir()
    (configured / "auth.json").write_text('{"token":"secret"}')
    (configured / "config.toml").write_text('[mcp_servers.danger]\ncommand="mutate"\n')
    monkeypatch.setenv("CODEX_HOME", str(configured))
    _fake_codex(fake_bin, json.dumps(APPROVE))
    result = _review("codex", tmp_path)
    assert result.verdict == "approve" and result.family == "codex"
    argv = _argv(fake_bin, "codex")
    assert argv[:4] == ["exec", "--sandbox", "read-only", "--cd"]
    assert argv[4] == str(tmp_path) and "--ephemeral" in argv
    assert argv[argv.index("-c") + 1] == "mcp_servers={}"
    assert argv[-1] == "-"
    isolated = (fake_bin / "codex.home").read_text()
    assert isolated != str(configured) and "loopzero-codex-" in isolated
    assert (fake_bin / "codex.auth").read_text() == '{"token":"secret"}'
    assert not (fake_bin / "codex.config").exists()
    assert "Do the thing" in (fake_bin / "codex.stdin").read_text()


def test_codex_request_changes(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, json.dumps(CHANGES))
    result = _review("codex", tmp_path)
    assert result.verdict == "request_changes" and len(result.findings) == 2
    assert json.loads(result.raw) == CHANGES


def test_codex_malformed_json(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, "Sure! Here is my review: it looks fine.")
    with pytest.raises(RunnerBadOutput) as info:
        _review("codex", tmp_path)
    assert "looks fine" in info.value.tail


def test_codex_auth_failure_text(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, "", exit_code=1, stderr="Error: Not logged in. Run codex login first.")
    with pytest.raises(RunnerAuthFailed):
        _review("codex", tmp_path)


def test_codex_missing_binary(fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))
    with pytest.raises(RunnerMissing):
        _review("codex", tmp_path)


def test_unknown_family_and_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _review("gemini", tmp_path)
    with pytest.raises(ValueError):
        review_with("claude", cwd=tmp_path, head="h", kind="full", diff="", task_text="")


@pytest.mark.parametrize(
    "finding",
    [
        {"severity": "critical"},
        {"severity": "critical", "path": None, "line": None, "title": 4, "body": "x"},
        {"severity": "critical", "path": None, "line": None, "title": "x", "body": []},
        {
            "severity": "critical", "path": None, "line": None,
            "title": "x", "body": "y", "extra": 1,
        },
    ],
)
def test_review_schema_is_strict(fake_bin: Path, tmp_path: Path, finding: dict) -> None:
    _fake_codex(fake_bin, json.dumps({"verdict": "request_changes", "findings": [finding]}))
    with pytest.raises(RunnerBadOutput):
        _review("codex", tmp_path)


def test_timeout_becomes_bad_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from loopzero import _proc, runners

    def timeout(*args, **kwargs):
        raise _proc.ProcTimeout("timed out", "partial reviewer output")

    monkeypatch.setattr(runners._proc, "run", timeout)
    with pytest.raises(RunnerBadOutput, match="timed out") as info:
        _review("claude", tmp_path)
    assert "partial reviewer output" in info.value.tail


# ----------------------------------------------------------------- family detection


def _commit(repo: Path, message: str) -> str:
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize(
    ("trailer", "expected"),
    [
        ("Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>", "claude"),
        ("Co-Authored-By: Codex <codex@openai.com>", "codex"),
        ("Co-Authored-By: GPT-5 <bot@example.com>", "codex"),
        ("Co-Authored-By: OpenAI Assistant <bot@example.com>", "codex"),
        ("Co-Authored-By: Jane Doe <jane@example.com>", None),
        ("", None),
    ],
)
def test_author_family(git_repo: Path, trailer: str, expected: str | None) -> None:
    head = _commit(git_repo, f"feat: x\n\n{trailer}" if trailer else "feat: x")
    assert author_family(git_repo, head) == expected
    assert author_family(git_repo, "HEAD") == expected


def test_author_family_bad_ref(git_repo: Path) -> None:
    with pytest.raises(RunnerBadOutput):
        author_family(git_repo, "no-such-ref")


def test_pick_reviewer() -> None:
    assert pick_reviewer(("claude", "codex"), "claude") == "codex"
    assert pick_reviewer(("claude", "codex"), "codex") == "claude"
    assert pick_reviewer(("claude", "codex"), None) == "claude"
    with pytest.raises(ValueError, match="no independent reviewer"):
        pick_reviewer(("claude",), "claude")
    with pytest.raises(ValueError):
        pick_reviewer((), None)
