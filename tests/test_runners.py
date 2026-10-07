"""Tests for loopzero.runners using fake ``claude``/``codex`` executables on PATH."""

from __future__ import annotations

import json
import shlex
import textwrap
from pathlib import Path

import pytest

from loopzero.runners import (
    RunnerAuthFailed,
    RunnerBadOutput,
    RunnerMissing,
    RunnerPromptTooLong,
    RunnerUsageLimit,
    author_families,
    build_prompt,
    review_with,
    split_diff,
)
from loopzero.sandbox import SANDBOX_HOME, SandboxUnavailable

from .conftest import git
from .test_sandbox import FAKE_BWRAP, _pairs, _real_bwrap_works

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
    assert "delta review" in prompt and "Be skeptical" in prompt


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
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _review("claude", repo).verdict == "approve"
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
    repo = tmp_path / "repo"
    repo.mkdir()
    _review("claude", repo)
    assert all(destination == SANDBOX_HOME for _, destination in _pairs(
        _bwrap_argv(fake_bwrap), "--bind"
    ))

    monkeypatch.setenv("CODEX_HOME", str(host_home / "missing-codex"))
    _fake_codex(fake_bin, json.dumps(APPROVE))
    _review("codex", repo)
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


@pytest.mark.parametrize("payload,exit_code", [
    ("Not logged in. Please run /login", 1),
    ({"type": "result", "subtype": "error", "is_error": True,
      "result": "Invalid API key · Please run /login"}, 0),
])
def test_claude_auth_failure_text_or_error_envelope(fake_bin: Path, tmp_path: Path, payload, exit_code) -> None:
    _fake_claude(fake_bin, payload, exit_code=exit_code)
    with pytest.raises(RunnerAuthFailed):
        _review("claude", tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_claude_expired_oauth_envelope(fake_bin: Path, tmp_path: Path, exit_code: int) -> None:
    envelope = {"is_error": True, "api_error_status": 401, "terminal_reason": "api_error",
                "result": "Failed to authenticate. API Error: 401 OAuth access token has expired. "
                          "Re-authenticate to continue."}
    _fake_claude(fake_bin, envelope, exit_code=exit_code)
    with pytest.raises(RunnerAuthFailed, match="claude: CLI is not authenticated"):
        _review("claude", tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("envelope", [
    {"api_error_status": status, "result": "Denied"} for status in (401, 403)
] + [{"terminal_reason": "api_error", "result": result}
     for result in ("Cannot authenticate", "OAuth failure", "Session expired", "401")])
def test_claude_auth_failure_envelope(fake_bin: Path, tmp_path: Path, exit_code, envelope) -> None:
    _fake_claude(fake_bin, envelope, exit_code=exit_code)
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
def test_auth_phrases_only_fail_on_nonzero_exit(fake_bin: Path, tmp_path: Path, family, message, exit_code) -> None:
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


@pytest.mark.parametrize("family", ["claude", "codex"])
def test_success_with_auth_words_is_not_auth_failure(fake_bin: Path, tmp_path: Path,
                                                     family: str) -> None:
    if family == "claude":
        _fake_claude(fake_bin, _claude_envelope(AUTH_WORDS))
    else:
        _fake_codex(fake_bin, json.dumps(AUTH_WORDS), stderr="warning: not authenticated to telemetry")
    result = _review(family, tmp_path)
    assert result.verdict == "approve" and result.findings[0].title == "Handle 401 Unauthorized"


def test_claude_nonzero_exit_is_bad_output(fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, "boom", exit_code=2)
    with pytest.raises(RunnerBadOutput, match="exited 2"):
        _review("claude", tmp_path)


@pytest.mark.parametrize("family,phrase,error", [
    ("claude", "Prompt is too long", RunnerPromptTooLong),
    ("claude", "prompt is too long", RunnerPromptTooLong),
    ("codex", "context_length_exceeded", RunnerPromptTooLong),
    ("codex", "maximum context length exceeded", RunnerPromptTooLong),
    ("codex", 'data: {"input_error_code":"input_too_large"}', RunnerPromptTooLong),
    ("claude", "You've reached your Fable limit. Switch to another model", RunnerUsageLimit),
    ("codex", "You've hit your usage limit. Try again at 3:05 PM.", RunnerUsageLimit),
])
def test_provider_errors_are_typed(fake_bin: Path, tmp_path: Path, family, phrase, error) -> None:
    if family == "claude":
        _fake_claude(fake_bin, phrase, exit_code=1)
    else:
        _fake_codex(fake_bin, "", exit_code=1, stderr=phrase)
    with pytest.raises(error):
        _review(family, tmp_path)


@pytest.mark.parametrize("family", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["missing", "nonexecutable", "shim-only", "shim-first", "ordinary"])
def test_reviewer_binary_selection(fake_bin: Path, git_repo: Path, monkeypatch, family, mode) -> None:
    shim = _script(fake_bin, "ai-accounts", "exit 99")
    if mode.startswith("shim"):
        (fake_bin / family).symlink_to(shim)
    elif mode == "nonexecutable":
        _script(fake_bin, family, "exit 99").chmod(0o644)
    real_dir = fake_bin / "versions"
    real_dir.mkdir()
    (fake_bin / "git").symlink_to("/usr/bin/git")
    if mode in {"shim-first", "ordinary"}:
        real = (_fake_claude(real_dir, _claude_envelope(APPROVE)) if family == "claude"
                else _fake_codex(real_dir, json.dumps(APPROVE)))
        real.rename(real_dir / "2.1.286")
        real.symlink_to(real_dir / "2.1.286")
        if mode == "ordinary":
            (fake_bin / family).symlink_to(real_dir / "2.1.286")
            real.unlink()
            _script(real_dir, family, "exit 99")
    monkeypatch.setenv("PATH", f"{fake_bin}:{real_dir}:/usr/bin:/bin" if mode in {"shim-first", "ordinary"} else str(fake_bin))
    def guard(config, *args):  # Reject shim selection before any executable can run.
        assert mode in {"shim-first", "ordinary"}
        assert config.sandbox_ro == (str(real_dir),)
    monkeypatch.setattr("loopzero.runners.sandbox.probe", guard)
    if mode in {"shim-first", "ordinary"}:
        assert _review(family, git_repo).verdict == "approve"
    else:
        with pytest.raises(RunnerMissing):
            _review(family, git_repo)


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


def test_reviewer_ro_paths_replace_binary_default(fake_bin: Path, tmp_path: Path, fake_bwrap: Path) -> None:
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


def test_empty_secure_storage_override_preserves_native_default(
    fake_bin, tmp_path, fake_bwrap, monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    assert _review("claude", repo).verdict == "approve"
    with pytest.raises(SandboxUnavailable, match="protected reviewer state"):
        _review("claude", repo, reviewer_ro_paths=(str(Path.home() / ".claude"),))


@pytest.mark.parametrize("state", [
    ".claude", ".codex", ".claude.json", "custom-claude", "custom-codex", "secure-storage",
])
@pytest.mark.parametrize("exposure", ["exact", "ancestor", "descendant", "alias", "repository"])
def test_review_refuses_additional_host_state_exposure(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch,
    state: str, exposure: str,
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host / "custom-claude"))
    monkeypatch.setenv("CODEX_HOME", str(host / "custom-codex"))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(host / "secure-storage"))
    protected = host / state
    protected.mkdir()
    (protected / "history.txt").write_text("private host history")
    child = protected / "child"
    child.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    exposed = {"exact": protected, "ancestor": host, "descendant": child,
               "alias": alias, "repository": protected}[exposure]
    repo = protected if exposure == "repository" else tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    with pytest.raises(SandboxUnavailable, match="protected reviewer state"):
        _review("claude", repo, reviewer_ro_paths=() if exposure == "repository" else (str(exposed),))
    assert not (fake_bin / "claude.stdin").exists()


@pytest.mark.parametrize("mode", ["implicit", "explicit", "state_file", "state_root"])
def test_review_mounts_only_implicit_executable_inside_state(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    state = host / ".codex"
    state.mkdir()
    native = state / "packages" / "claude"
    native.parent.mkdir()
    if mode == "state_file":
        native = host / ".claude.json"
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CODEX_HOME", str(native if mode == "state_root" else state))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host / ".claude"))
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    (fake_bin / "claude").replace(native)
    (fake_bin / "claude").symlink_to(native)
    repo = tmp_path / "repo"
    repo.mkdir()
    if mode == "implicit":
        assert _review("claude", repo).verdict == "approve"
        assert _pairs(_bwrap_argv(fake_bwrap), "--ro-bind-try") == [(str(native), str(native))]
        assert (str(state), str(state)) not in _pairs(_bwrap_argv(fake_bwrap), "--ro-bind")
        return
    with pytest.raises(SandboxUnavailable, match="protected reviewer state"):
        _review("claude", repo, reviewer_ro_paths=(str(native),) if mode == "explicit" else ())
    assert not (fake_bin / "claude.stdin").exists()


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_review_executes_state_binary_without_exposing_sibling_history(
    fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = tmp_path / "host"
    state = host / ".codex"
    native = state / "packages" / "claude"
    native.parent.mkdir(parents=True)
    history = native.parent / "history.txt"
    history.write_text("private history marker")
    native.write_text("#!/bin/sh\ncat >/dev/null\ntest ! -e " + shlex.quote(str(history))
                      + " || exit 70\nprintf '%s\\n' "
                      + shlex.quote(json.dumps(_claude_envelope(APPROVE))) + "\n")
    native.chmod(0o755)
    (fake_bin / "bwrap").unlink()  # Override this module's autouse fake for the kernel proof.
    (fake_bin / "claude").symlink_to(native)
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CODEX_HOME", str(state))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host / ".claude"))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _review("claude", repo).verdict == "approve"
    assert history.read_text() == "private history marker"
    with pytest.raises(SandboxUnavailable, match="protected reviewer state"):
        _review("claude", repo, reviewer_ro_paths=(str(native.parent),))


@pytest.mark.parametrize("mount", ["system", "git"])
def test_review_denies_state_exposure_from_non_runtime_mounts(
    fake_bin: Path, tmp_path: Path, fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch, mount: str,
) -> None:
    from loopzero import sandbox

    host = tmp_path / "host"
    state = host / ".codex"
    state.mkdir(parents=True)
    (state / "history.txt").write_text("private history marker")
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("CODEX_HOME", str(state))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host / ".claude"))
    repo = tmp_path / "repo"
    repo.mkdir()
    if mount == "system":
        monkeypatch.setattr(sandbox, "SYSTEM_RO", (*sandbox.SYSTEM_RO, str(host)))
    else:
        git(repo, "init", "-q", "--separate-git-dir", str(state / "git"))
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    with pytest.raises(SandboxUnavailable, match="protected reviewer state"):
        _review("claude", repo)
    assert not (fake_bin / "claude.stdin").exists()


def test_missing_bwrap_fails_closed(fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _review("codex", repo, model="gpt-5.6-luna", effort="high")
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
    assert argv[argv.index("--cd") + 1] == str(repo.resolve()) and "--ephemeral" in argv
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
def test_author_families(git_repo: Path, trailer: str, expected: str | None) -> None:
    base = git(git_repo, "rev-parse", "HEAD").strip()
    head = _commit(git_repo, f"feat: x\n\n{trailer}" if trailer else "feat: x")
    assert author_families(git_repo, head, base) == ({expected} if expected else set())
    assert author_families(git_repo, "HEAD", base) == ({expected} if expected else set())


def test_author_families_bad_ref(git_repo: Path) -> None:
    with pytest.raises(RunnerBadOutput):
        author_families(git_repo, "no-such-ref", "HEAD")


def test_author_families_excludes_base_and_includes_all_lineage_trailers(git_repo: Path) -> None:
    base = _commit(git_repo, "base\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    _commit(git_repo, "child\n\nCo-Authored-By: Codex <codex@openai.com>")
    head = _commit(git_repo, "human repair\n\nCo-Authored-By: Jane <jane@example.com>")
    assert author_families(git_repo, head, base) == {"codex"}
    head = _commit(git_repo, "repair\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
                   "Co-Authored-By: GPT <gpt@openai.com>")
    assert author_families(git_repo, head, base) == {"claude", "codex"}
