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
    RunnerPromptTooLong,
    RunnerUsageLimit,
    author_family,
    build_prompt,
    pick_reviewer,
    review_with,
    split_diff,
)
from loopzero.sandbox import SANDBOX_HOME, SandboxUnavailable

from .conftest import git
from .test_sandbox import FAKE_BWRAP

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


def _bwrap_argv(log: Path) -> list[str]:
    return log.read_text().splitlines()


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, arg in enumerate(argv) if arg == flag]


@pytest.fixture(autouse=True)
def fake_bwrap(fake_tool, tmp_path: Path) -> Path:
    log = tmp_path / "review-bwrap.argv"
    fake_tool("bwrap", FAKE_BWRAP.format(log=log))
    return log


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


def _fake_codex(
    bin_dir: Path,
    last_message: str,
    *,
    exit_code: int = 0,
    stderr: str = "",
    session_id: str = "codex-session-123",
    tokens: int = 123,
) -> Path:
    """Fake ``codex`` that checks argv shape and writes ``last_message`` to -o file."""
    return _script(
        bin_dir,
        "codex",
        f"""
        if [ "$1" = exec ] && [ "$2" = --help ]; then
          echo '      --ignore-user-config'
          exit 0
        fi
        codex_home="${{CODEX_HOME:-$HOME/.codex}}"
        printf '%s\\0' "$@" > "{bin_dir}/codex.argv"
        printf '%s' "$codex_home" > "{bin_dir}/codex.home"
        [ -e "$codex_home/config.toml" ] && echo yes > "{bin_dir}/codex.config" || true
        [ ! -f "$codex_home/auth.json" ] || cat "$codex_home/auth.json" > "{bin_dir}/codex.auth"
        [ ! -f "$codex_home/auth.json" ] || printf refreshed > "$codex_home/auth.json"
        cat > "{bin_dir}/codex.stdin"
        [ "$1" = exec ] || {{ echo "expected exec subcommand" >&2; exit 64; }}
        out=""; schema=""
        while [ $# -gt 0 ]; do
          case "$1" in
            --output-last-message) out="$2"; shift;;
            --output-schema) schema="$2"; shift;;
            --sandbox) [ "$2" = danger-full-access ] || {{ echo "bad sandbox $2" >&2; exit 64; }}; shift;;
          esac
          shift
        done
        [ -n "$out" ] || {{ echo "missing -o" >&2; exit 64; }}
        [ -f "$schema" ] || {{ echo "missing schema file" >&2; exit 64; }}
        [ -n "{stderr}" ] && echo "{stderr}" >&2
        cat > "$out" <<'MSG'
        {last_message}
        MSG
        echo '{{"type":"thread.started","thread_id":"{session_id}"}}'
        echo '{{"type":"turn.completed","usage":{{"input_tokens":{tokens},"output_tokens":2}}}}'
        exit {exit_code}
        """,
    )


def _claude_envelope(payload: object, **extra: object) -> dict[str, object]:
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": json.dumps(payload), "structured_output": payload,
            "session_id": "claude-session-123", "usage": {"input_tokens": 10},
            "total_cost_usd": 0.01, "duration_api_ms": 25, **extra}


def _review(family: str, cwd: Path, diff: str = "--- a\n+++ b\n+x\n", **options):
    if not (cwd / ".git").exists():
        git(cwd, "init", "-q")
    return review_with(family, cwd=cwd, head="abc123", kind="primary",
                       diff=diff, task_text="Do the thing", **options)


# ----------------------------------------------------------------- prompt


def test_prompt_contains_task_diff_and_json_contract() -> None:
    prompt = build_prompt(kind="delta", head="deadbeef", task_text="Objective", diff="+line")
    assert "Objective" in prompt and "+line" in prompt and "deadbeef" in prompt
    assert '"verdict": "approve"|"request_changes"' in prompt
    assert "critical" in prompt and "important" in prompt and "suggestion" in prompt
    assert "delta review" in prompt


# ----------------------------------------------------------------- claude


def test_claude_approve(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE, modelUsage={"claude-sonnet-4-6": {}}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    result = _review("claude", tmp_path, model="opus", effort="medium")
    assert result.verdict == "approve" and result.findings == ()
    assert result.family == "claude" and result.head == "abc123" and result.kind == "primary"
    assert result.model == "claude-sonnet-4-6" and result.effort == "medium" and result.duration_s is not None
    assert json.loads(result.raw)["structured_output"] == APPROVE
    argv = _argv(fake_bin, "claude")
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "medium"
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
    sandbox_argv = _bwrap_argv(fake_bwrap)
    options = sandbox_argv[: sandbox_argv.index("--")]
    assert "--share-net" in options
    assert (str(tmp_path.resolve()), str(tmp_path.resolve())) in _pairs(options, "--ro-bind")
    assert _pairs(options, "--ro-bind-try") == [(str(fake_bin.resolve()),) * 2]
    assert _pairs(options, "--bind")[0][1] == SANDBOX_HOME


def test_reviewer_sandbox_binds_resolv_conf(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path
) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    _review("claude", tmp_path)
    argv = _bwrap_argv(fake_bwrap)
    resolv_conf = Path("/etc/resolv.conf")
    resolved = resolv_conf.resolve()
    expected = (
        (str(resolved.parent), str(resolved.parent))
        if resolv_conf.is_symlink()
        and any(resolved.is_relative_to(root) for root in (Path("/tmp"), Path("/run")))
        else (str(resolved), str(resolv_conf))
    )
    assert expected in _pairs(argv, "--ro-bind")


def test_claude_binds_config_and_state_read_write_without_copying(
    fake_bin: Path,
    tmp_path: Path,
    fake_bwrap: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    configured = tmp_path / "configured-claude"
    configured.mkdir()
    (configured / ".credentials.json").write_text('{"token":"secret"}')
    (configured / "settings.json").write_text('{"danger":true}')
    state = host_home / ".claude.json"
    state.write_text('{"oauth":"live"}')
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    monkeypatch.setattr("loopzero.runners.shutil.copyfile", lambda *args: pytest.fail("copied auth"))
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    assert _review("claude", tmp_path).verdict == "approve"
    binds = _pairs(_bwrap_argv(fake_bwrap), "--bind")
    assert (str(configured.resolve()), f"{SANDBOX_HOME}/.claude") in binds
    assert (str(state.resolve()), f"{SANDBOX_HOME}/.claude.json") in binds


def test_reviewer_does_not_bind_missing_auth_paths(
    fake_bin: Path,
    tmp_path: Path,
    fake_bwrap: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_home = tmp_path / "empty-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host_home / "missing-claude"))
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    _review("claude", tmp_path)
    assert all(destination == SANDBOX_HOME for _, destination in _pairs(
        _bwrap_argv(fake_bwrap), "--bind"
    ))

    monkeypatch.setenv("CODEX_HOME", str(host_home / "missing-codex"))
    _fake_codex(fake_bin, json.dumps(APPROVE))
    _review("codex", tmp_path)
    assert all(destination == SANDBOX_HOME for _, destination in _pairs(
        _bwrap_argv(fake_bwrap), "--bind"
    ))


def test_linked_worktree_and_git_directory_are_read_only(
    git_repo: Path, tmp_path: Path, fake_bin: Path, fake_bwrap: Path
) -> None:
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", str(linked), "-b", "review")
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    assert _review("claude", linked).verdict == "approve"
    binds = _pairs(_bwrap_argv(fake_bwrap), "--ro-bind")
    assert (str(linked.resolve()),) * 2 in binds
    assert (str((git_repo / ".git").resolve()),) * 2 in binds


def test_claude_request_changes_escalates_verdict(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, _claude_envelope(CHANGES))
    result = _review("claude", tmp_path)
    assert result.verdict == "request_changes"
    assert [f.severity for f in result.findings] == ["important", "suggestion"]
    assert result.findings[0].path == "a.py" and result.findings[0].line == 3
    assert result.findings[1].path is None and result.findings[1].line is None


def test_claude_falls_back_to_result_text(fake_bin: Path, tmp_path: Path) -> None:
    env = _claude_envelope(APPROVE)
    env.pop("structured_output")
    env["result"] = "```json\n" + json.dumps(APPROVE) + "\n```"
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


@pytest.mark.parametrize("exit_code", [0, 1])
def test_claude_expired_oauth_envelope(fake_bin: Path, tmp_path: Path, exit_code: int) -> None:
    envelope = {
        "is_error": True,
        "api_error_status": 401,
        "terminal_reason": "api_error",
        "result": "Failed to authenticate. API Error: 401 OAuth access token has expired. "
                  "Re-authenticate to continue.",
    }
    _fake_claude(fake_bin, envelope, exit_code=exit_code)
    with pytest.raises(RunnerAuthFailed, match="claude: CLI is not authenticated"):
        _review("claude", tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("status", [401, 403])
def test_claude_auth_failure_api_status(
    fake_bin: Path, tmp_path: Path, exit_code: int, status: int,
) -> None:
    _fake_claude(fake_bin, {"api_error_status": status, "result": "Denied"}, exit_code=exit_code)
    with pytest.raises(RunnerAuthFailed):
        _review("claude", tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("result", ["Cannot authenticate", "OAuth failure", "Session expired", "401"])
def test_claude_auth_failure_terminal_reason(
    fake_bin: Path, tmp_path: Path, exit_code: int, result: str,
) -> None:
    _fake_claude(fake_bin, {"terminal_reason": "api_error", "result": result}, exit_code=exit_code)
    with pytest.raises(RunnerAuthFailed):
        _review("claude", tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("status,match", [(500, "claude: (error|exited)"), (429, "usage limit reached")])
def test_claude_non_auth_api_error(fake_bin: Path, tmp_path: Path, exit_code, status, match) -> None:
    _fake_claude(fake_bin, {"is_error": True, "api_error_status": status,
                           "terminal_reason": "api_error", "result": "Internal server error"},
                 exit_code=exit_code)
    with pytest.raises(RunnerBadOutput, match=match):
        _review("claude", tmp_path)


@pytest.mark.parametrize("family,message", [
    ("claude", "OAuth access token has expired"),
    ("claude", "Failed to authenticate"),
    ("codex", "401 Unauthorized"),
    ("codex", "token expired"),
    ("codex", "Please run codex login"),
])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_auth_phrases_only_fail_on_nonzero_exit(
    fake_bin: Path, tmp_path: Path, family: str, message: str, exit_code: int,
) -> None:
    if family == "claude":
        _fake_claude(fake_bin, _claude_envelope(APPROVE, result=message), exit_code=exit_code)
    else:
        _fake_codex(fake_bin, json.dumps(APPROVE), stderr=message, exit_code=exit_code)
    if exit_code:
        with pytest.raises(RunnerAuthFailed):
            _review(family, tmp_path)
    else:
        assert _review(family, tmp_path).verdict == "approve"


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


@pytest.mark.parametrize(
    "family,phrase,error",
    [
        ("claude", "Prompt is too long", RunnerPromptTooLong),
        ("claude", "prompt is too long", RunnerPromptTooLong),
        ("codex", "context_length_exceeded", RunnerPromptTooLong),
        ("codex", "maximum context length exceeded", RunnerPromptTooLong),
        ("claude", "You've reached your Fable limit. Switch to another model", RunnerUsageLimit),
        ("codex", "You've hit your usage limit. Try again at 3:05 PM.", RunnerUsageLimit),
    ],
)
def test_provider_errors_are_typed(
    fake_bin: Path, tmp_path: Path, family: str, phrase: str, error: type
) -> None:
    if family == "claude":
        _fake_claude(fake_bin, phrase, exit_code=1)
    else:
        _fake_codex(fake_bin, "", exit_code=1, stderr=phrase)
    with pytest.raises(error):
        _review(family, tmp_path)


def test_claude_missing_binary(
    fake_bin: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))  # nothing else on PATH
    with pytest.raises(RunnerMissing):
        _review("claude", git_repo)


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


def test_secrets_never_appear_in_bwrap_argv(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    _review("claude", tmp_path)
    argv = fake_bwrap.read_text().splitlines()
    assert "sk-secret" not in argv and "ANTHROPIC_API_KEY" not in argv
    assert "--clearenv" not in argv


def test_reviewer_ro_paths_replace_binary_default(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path
) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    allowed = tmp_path / "runtime"
    allowed.mkdir()
    if not (tmp_path / ".git").exists():
        git(tmp_path, "init", "-q")
    review_with(
        "claude", cwd=tmp_path, head="abc123", kind="primary", diff="", task_text="x",
        reviewer_ro_paths=(str(allowed),),
    )
    argv = _bwrap_argv(fake_bwrap)
    assert _pairs(argv, "--ro-bind-try") == [(str(allowed), str(allowed))]
    assert str(fake_bin) not in [path for pair in _pairs(argv, "--ro-bind-try") for path in pair]


def test_missing_bwrap_fails_closed(
    fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    (fake_bin / "bwrap").unlink()
    monkeypatch.setattr(
        "loopzero.runners.shutil.which",
        lambda name: None if name == "bwrap" else str(fake_bin / name),
    )
    with pytest.raises(SandboxUnavailable, match="bwrap not found"):
        _review("claude", tmp_path)


def test_bwrap_probe_failure_fails_closed(fake_bin: Path, tmp_path: Path, fake_tool) -> None:
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    fake_tool("bwrap", "echo 'namespace denied' >&2\nexit 1\n")
    with pytest.raises(SandboxUnavailable, match="namespace denied"):
        _review("claude", tmp_path)
    assert not (fake_bin / "claude.stdin").exists()


# ----------------------------------------------------------------- codex


def test_codex_approve(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured-codex"
    configured.mkdir()
    (configured / "auth.json").write_text('{"token":"secret"}')
    (configured / "config.toml").write_text('[mcp_servers.danger]\ncommand="mutate"\n')
    monkeypatch.setenv("CODEX_HOME", str(configured))
    monkeypatch.setattr("loopzero.runners.shutil.copyfile", lambda *args: pytest.fail("copied auth"))
    _fake_codex(fake_bin, json.dumps(APPROVE))
    result = _review("codex", tmp_path, model="gpt-5.6-luna", effort="high")
    assert result.verdict == "approve" and result.family == "codex"
    assert result.model == "gpt-5.6-luna" and result.effort == "high"
    assert result.duration_s is not None
    argv = _argv(fake_bin, "codex")
    assert argv[0] == "exec"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=high" in argv
    # Codex's inner bwrap cannot nest inside loop-zero's; the outer sandbox bounds it.
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert "--ignore-user-config" in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path.resolve()) and "--ephemeral" in argv
    assert "mcp_servers={}" in argv
    assert argv[-1] == "-"
    isolated = (fake_bin / "codex.home").read_text()
    assert isolated != str(configured) and "loopzero-review-home-" in isolated
    assert not (fake_bin / "codex.config").exists()
    assert "Do the thing" in (fake_bin / "codex.stdin").read_text()
    assert argv[argv.index("--output-schema") + 1].endswith("/schema.json")
    assert argv[argv.index("--output-last-message") + 1].endswith("/last.json")
    sandbox_argv = _bwrap_argv(fake_bwrap)
    writable = _pairs(sandbox_argv, "--bind")
    auth_bind = (str((configured / "auth.json").resolve()), f"{SANDBOX_HOME}/.codex/auth.json")
    assert auth_bind in writable
    assert all(dst in (SANDBOX_HOME, auth_bind[1]) for _, dst in writable)
    command = sandbox_argv[sandbox_argv.index("--") + 1 :]
    assert command[command.index("--output-schema") + 1] == f"{SANDBOX_HOME}/schema.json"
    assert command[command.index("--output-last-message") + 1] == f"{SANDBOX_HOME}/last.json"


def test_codex_request_changes(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, json.dumps(CHANGES), stderr="model: fallback")
    result = _review("codex", tmp_path)
    assert result.verdict == "request_changes" and len(result.findings) == 2
    assert result.model == "fallback"
    assert json.loads(result.raw)["session_id"] == "codex-session-123"
    assert result.provenance["token_usage"]["input_tokens"] == 123


def test_codex_malformed_json(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, "Sure! Here is my review: it looks fine.")
    with pytest.raises(RunnerBadOutput) as info:
        _review("codex", tmp_path)
    assert "looks fine" in info.value.tail


def test_codex_auth_failure_text(fake_bin: Path, tmp_path: Path) -> None:
    _fake_codex(fake_bin, "", exit_code=1, stderr="Error: Not logged in. Run codex login first.")
    with pytest.raises(RunnerAuthFailed):
        _review("codex", tmp_path)


def test_codex_missing_binary(
    fake_bin: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))
    with pytest.raises(RunnerMissing):
        _review("codex", git_repo)


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


def test_timeout_becomes_bad_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_bin: Path, fake_bwrap: Path
) -> None:
    from loopzero import _proc, runners

    def timeout(*args, **kwargs):
        raise _proc.ProcTimeout("timed out", "partial reviewer output")

    _fake_claude(fake_bin, {})
    monkeypatch.setattr(runners, "_git_dir", lambda cwd, flag, env: cwd)
    monkeypatch.setattr(runners.sandbox, "probe", lambda config, cwd, prefix: None)
    monkeypatch.setattr(runners._proc, "run", timeout)
    with pytest.raises(RunnerBadOutput, match="timed out") as info:
        _review("claude", tmp_path)
    assert "partial reviewer output" in info.value.tail


def test_split_diff_respects_budget_and_summarizes_deleted_file() -> None:
    deleted = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n-one\n-two\n-three\n"
    )
    added = "diff --git a/new.py b/new.py\n" + "+x\n" * 60
    chunks = split_diff(deleted + added, 100)
    assert all(len(chunk.encode()) <= 100 for chunk in chunks)
    assert any("gone.py: deleted, 3 lines" in chunk for chunk in chunks)


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
