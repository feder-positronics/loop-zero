"""Model review adapters: thin subprocess wrappers around ``claude -p`` and ``codex exec``.

Both adapters share one prompt builder and one JSON findings schema, and both
return a :class:`~loopzero.types.ReviewResult`. Nothing here retries.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from loopzero import _proc, sandbox
from loopzero.types import Config, Finding, LoopZeroError, ReviewResult

FAMILIES = ("claude", "codex")
SEVERITIES = ("critical", "important", "suggestion")
BLOCKING = ("critical", "important")
TAIL_CHARS = 2000

# Extra environment each CLI needs to authenticate; forwarded only if present.
CLAUDE_AUTH_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
CODEX_AUTH_ENV = ("OPENAI_API_KEY",)
BASE_ENV = ("PATH", "LANG", "LC_ALL", "TERM")
_CODEX_MODEL_RE = re.compile(r"^\s*model\s*:\s*(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)

_AUTH_PATTERNS = (
    re.compile(r"not logged in", re.IGNORECASE),
    re.compile(r"please run /login", re.IGNORECASE),
    re.compile(r"please (?:run )?`?codex login`?", re.IGNORECASE),
    re.compile(r"invalid api key", re.IGNORECASE),
    re.compile(r"authentication[_ ]error", re.IGNORECASE),
    re.compile(r"OAuth access token has expired|Failed to authenticate|token expired", re.IGNORECASE),
    re.compile(r"\b401\b.*unauthori[sz]ed|unauthori[sz]ed.*\b401\b", re.IGNORECASE),
    re.compile(r"login required|not authenticated|missing credentials", re.IGNORECASE),
)

REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "path", "line", "title", "body"],
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "path": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        },
    },
}


class RunnerError(LoopZeroError):
    """Base class for review runner failures."""


class RunnerMissing(RunnerError):
    """The CLI binary is not on PATH."""


class RunnerAuthFailed(RunnerError):
    """The CLI reported that it is not authenticated; retrying cannot help."""


class RunnerBadOutput(RunnerError):
    """The CLI exited nonzero or returned output that is not a valid review."""

    def __init__(self, message: str, tail: str = "") -> None:
        super().__init__(f"{message}\n--- output tail ---\n{tail}" if tail else message)
        self.tail = tail


class RunnerPromptTooLong(RunnerBadOutput):
    """The provider rejected the prompt for exceeding its context window."""


_TOO_LONG_PATTERNS = (
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"context_length_exceeded", re.IGNORECASE),
    re.compile(r"maximum context length", re.IGNORECASE),
)


# --------------------------------------------------------------------------- prompt


def build_prompt(*, kind: str, head: str, task_text: str, diff: str) -> str:
    """Return the shared review prompt for either family."""
    scope = (
        "This is the primary review of the whole change."
        if kind == "primary"
        else "This is a delta review: only review the diff below, which covers "
        "the commits since the previous review."
    )
    return "\n".join(
        (
            "You are an independent code reviewer. Read only: do not edit files.",
            f"Reviewing commit {head}. {scope}",
            "",
            "## Task",
            task_text.strip(),
            "",
            "## Diff",
            "```diff",
            diff.rstrip("\n"),
            "```",
            "",
            "## Output",
            "Return ONLY a JSON object, no prose and no code fences, matching:",
            '{"verdict": "approve"|"request_changes", "findings": [{"severity": '
            + '"critical"|"important"|"suggestion", "path": string|null, '
            + '"line": integer|null, "title": string, "body": string}]}',
            "verdict MUST be \"request_changes\" if any finding is critical or important.",
            'Use "suggestion" for non-blocking remarks. Report only what you can justify.',
        )
    )


# --------------------------------------------------------------------------- parsing


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:]


def _looks_like_auth_failure(text: str) -> bool:
    return any(p.search(text) for p in _AUTH_PATTERNS)


def _claude_envelope_auth_failure(envelope: object) -> bool:
    if not isinstance(envelope, dict):
        return False
    return envelope.get("api_error_status") in (401, 403) or (
        envelope.get("terminal_reason") == "api_error"
        and re.search(r"authenticate|OAuth|expired|401", str(envelope.get("result", "")),
                      re.IGNORECASE) is not None
    )


def _looks_too_long(text: str) -> bool:
    return any(p.search(text) for p in _TOO_LONG_PATTERNS)


def _extract_json_object(text: str) -> object:
    """Parse ``text`` as JSON, tolerating code fences or surrounding prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def parse_review(payload: object, *, family: str, head: str, kind: str, raw: str) -> ReviewResult:
    """Validate a decoded review object against the findings schema."""
    if not isinstance(payload, dict):
        raise RunnerBadOutput(f"{family}: review is not a JSON object", _tail(raw))
    if set(payload) != {"verdict", "findings"}:
        raise RunnerBadOutput(f"{family}: review has unexpected or missing keys", _tail(raw))
    verdict, items = payload["verdict"], payload["findings"]
    if (
        not isinstance(verdict, str)
        or verdict not in ("approve", "request_changes")
        or not isinstance(items, list)
    ):
        raise RunnerBadOutput(f"{family}: review lacks verdict/findings", _tail(raw))
    findings: list[Finding] = []
    for item in items:
        required = {"severity", "path", "line", "title", "body"}
        if not isinstance(item, dict) or set(item) != required:
            raise RunnerBadOutput(f"{family}: finding has unexpected or missing keys", _tail(raw))
        if not isinstance(item["severity"], str) or item["severity"] not in SEVERITIES:
            raise RunnerBadOutput(f"{family}: finding has bad severity", _tail(raw))
        path, line = item["path"], item["line"]
        if path is not None and not isinstance(path, str):
            raise RunnerBadOutput(f"{family}: finding path is not a string", _tail(raw))
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            raise RunnerBadOutput(f"{family}: finding line is not an integer", _tail(raw))
        if not isinstance(item["title"], str) or not isinstance(item["body"], str):
            raise RunnerBadOutput(f"{family}: finding title/body must be strings", _tail(raw))
        findings.append(
            Finding(
                severity=item["severity"],
                path=path,
                line=line,
                title=item["title"],
                body=item["body"],
            )
        )
    if any(f.severity in BLOCKING for f in findings):
        verdict = "request_changes"
    return ReviewResult(
        family=family, head=head, kind=kind, verdict=verdict, findings=tuple(findings), raw=raw
    )


def _run(
    argv: list[str],
    *,
    cwd: Path,
    prompt: str,  # delivered on stdin
    extra_env: dict[str, str],
    env_allowlist: tuple[str, ...],
    timeout: int,
    family: str,
) -> _proc.Completed:
    try:
        done = _proc.run(
            argv,
            cwd=cwd,
            env_allowlist=env_allowlist,
            extra_env=extra_env,
            timeout=timeout,
            input=prompt,
        )
    except (_proc.ToolMissing, FileNotFoundError) as exc:
        raise RunnerMissing(f"{family}: {argv[0]!r} not found on PATH") from exc
    except _proc.ProcTimeout as exc:
        raise RunnerBadOutput(f"{family}: timed out after {timeout:g}s", _tail(exc.output)) from exc
    if done.exit_code != 0:
        combined = f"{done.stdout}\n{done.stderr}"
        envelope = None
        if family == "claude":
            try:
                envelope = _extract_json_object(done.stdout)
            except (json.JSONDecodeError, ValueError):
                pass
        if _claude_envelope_auth_failure(envelope) or _looks_like_auth_failure(combined):
            raise RunnerAuthFailed(f"{family}: CLI is not authenticated\n{_tail(combined)}")
        if _looks_too_long(combined):
            raise RunnerPromptTooLong(f"{family}: prompt is too long", _tail(combined))
        raise RunnerBadOutput(f"{family}: exited {done.exit_code}", _tail(combined))
    return done


_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


def _review_config(family: str, binary: Path, ro_paths: tuple[str, ...]) -> Config:
    """Build a networked, read-only sandbox config independent of check settings."""
    return Config(
        repo="review/sandbox",
        base_branch="main",
        checks=(),
        required_ci=(),
        merge_strategy="squash",
        reviewers=(family,),
        network=True,
        env_allowlist=BASE_ENV,
        sandbox_ro=ro_paths or (str(binary.resolve().parent),),
        writable=(),
        scratch=(),
        env=(),
    )


def _git_dir(cwd: Path, flag: str, env_allowlist: tuple[str, ...]) -> Path:
    done = _proc.run(
        ["git", "rev-parse", flag], cwd=cwd, env_allowlist=env_allowlist, timeout=30
    )
    if done.exit_code != 0:
        raise RunnerBadOutput(f"git rev-parse {flag} exited {done.exit_code}", _tail(done.stderr))
    raw = Path(done.stdout.strip())
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def _sandbox_prefix(
    family: str, cwd: Path, home: Path, ro_paths: tuple[str, ...]
) -> tuple[list[str], Config, Path]:
    binary_name = shutil.which(family)
    if binary_name is None:
        raise RunnerMissing(f"{family}: {family!r} not found on PATH")
    binary = Path(binary_name).resolve()
    config = _review_config(family, binary, ro_paths)
    if shutil.which("bwrap") is None:
        raise sandbox.SandboxUnavailable("bwrap not found on PATH (install bubblewrap)")
    for exposed in config.sandbox_ro:
        if home.resolve().is_relative_to(Path(exposed).resolve()):
            raise sandbox.SandboxUnavailable(
                f"sandbox path {exposed!r} would expose private reviewer directories"
            )
    common_dir = _git_dir(cwd, "--git-common-dir", config.env_allowlist)
    git_dir = _git_dir(cwd, "--git-dir", config.env_allowlist)
    prefix = sandbox.bwrap_argv(config, cwd, home, common_dir, git_dir, clearenv=False)
    sandbox.probe(config, cwd, prefix)
    return prefix, config, binary


def _copy_auth(family: str, home: Path) -> None:
    if family == "claude":
        source_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        source = source_dir / ".credentials.json"
        destination = home / ".claude" / ".credentials.json"
    else:
        source_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        source = source_dir / "auth.json"
        destination = home / ".codex" / "auth.json"
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _claude_provenance(envelope: dict, raw: str) -> dict[str, object]:
    required = ("session_id", "usage", "total_cost_usd", "duration_api_ms")
    if any(name not in envelope for name in required):
        raise RunnerBadOutput("claude: result envelope lacks provenance", _tail(raw))
    session_id = envelope["session_id"]
    usage = envelope["usage"]
    cost = envelope["total_cost_usd"]
    duration = envelope["duration_api_ms"]
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(usage, dict)
        or isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
    ):
        raise RunnerBadOutput("claude: result envelope has invalid provenance", _tail(raw))
    return {
        "family": "claude",
        "session_id": session_id,
        "session_ids": [session_id],
        "usage": usage,
        "total_cost_usd": cost,
        "duration_api_ms": duration,
    }


def _json_lines(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _codex_provenance(stdout: str, stderr: str, message: str) -> tuple[dict[str, object], str]:
    events = _json_lines(stdout)
    session_id = next(
        (
            str(event[key])
            for event in events
            for key in ("thread_id", "session_id")
            if isinstance(event.get(key), str) and event[key]
        ),
        None,
    )
    usage = next(
        (event["usage"] for event in reversed(events) if isinstance(event.get("usage"), dict)),
        None,
    )
    session_match = re.search(
        r"(?:session|thread)(?:\s+id)?\s*[:=]\s*([A-Za-z0-9_-]{6,})", stderr,
        re.IGNORECASE,
    )
    tokens_match = re.search(
        r"tokens used\s*(?:[:=]|\r?\n)\s*([0-9][0-9,]*)", stderr,
        re.IGNORECASE,
    )
    session_id = session_id or (session_match.group(1) if session_match else None)
    usage = usage or (
        {"total_tokens": int(tokens_match.group(1).replace(",", ""))}
        if tokens_match
        else None
    )
    if not session_id or not isinstance(usage, dict):
        raise RunnerBadOutput(
            "codex: execution output lacks session/token provenance", _tail(stdout + stderr)
        )
    envelope = {
        "type": "codex_exec_result",
        "session_id": session_id,
        "token_usage": usage,
        "final_message": message,
        "events": events,
    }
    provenance = {
        "family": "codex",
        "session_id": session_id,
        "session_ids": [session_id],
        "token_usage": usage,
    }
    return provenance, json.dumps(envelope, separators=(",", ":"))


# --------------------------------------------------------------------------- adapters


def _review_claude(
    prompt: str, *, cwd: Path, head: str, kind: str, timeout: int,
    ro_paths: tuple[str, ...],
) -> ReviewResult:
    argv = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(REVIEW_SCHEMA, separators=(",", ":")),
        "--permission-mode",
        "plan",
        "--tools",
        "Read,Grep,Glob",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disallowedTools",
        "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch",
    ]
    with tempfile.TemporaryDirectory(prefix="loopzero-review-home-") as tmp:
        home = Path(tmp)
        _copy_auth("claude", home)
        prefix, config, binary = _sandbox_prefix("claude", cwd, home, ro_paths)
        argv[0] = str(binary)
        done = _run(
            [*prefix, *argv], cwd=cwd, prompt=prompt, extra_env={},
            env_allowlist=(*config.env_allowlist, *CLAUDE_AUTH_ENV), timeout=timeout,
            family="claude",
        )
    raw = done.stdout
    try:
        envelope = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerBadOutput("claude: stdout is not JSON", _tail(raw)) from exc
    if not isinstance(envelope, dict):
        raise RunnerBadOutput("claude: result envelope is not an object", _tail(raw))
    if _claude_envelope_auth_failure(envelope):
        raise RunnerAuthFailed(f"claude: CLI is not authenticated\n{_tail(raw)}")
    if envelope.get("is_error") or envelope.get("subtype", "success") != "success":
        text = str(envelope.get("result", ""))
        if _looks_like_auth_failure(text):
            raise RunnerAuthFailed(f"claude: CLI is not authenticated\n{_tail(text)}")
        if _looks_too_long(text) or _looks_too_long(raw):
            raise RunnerPromptTooLong("claude: prompt is too long", _tail(raw))
        raise RunnerBadOutput(f"claude: {envelope.get('subtype', 'error')}", _tail(raw))
    payload = envelope.get("structured_output")
    if payload is None:
        try:
            payload = _extract_json_object(str(envelope.get("result", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            raise RunnerBadOutput("claude: result text is not JSON", _tail(raw)) from exc
    usage = envelope.get("modelUsage")
    models = tuple(key for key in usage if isinstance(key, str)) if isinstance(usage, dict) else ()
    model = ", ".join(models) or None
    provenance = _claude_provenance(envelope, raw)
    return replace(
        parse_review(payload, family="claude", head=head, kind=kind, raw=raw),
        model=model, duration_s=done.duration_s, provenance=provenance,
    )


def _review_codex(
    prompt: str, *, cwd: Path, head: str, kind: str, timeout: int,
    ro_paths: tuple[str, ...],
) -> ReviewResult:
    with tempfile.TemporaryDirectory(prefix="loopzero-review-home-") as tmp:
        home = Path(tmp)
        _copy_auth("codex", home)
        schema_file = home / "schema.json"
        last_file = home / "last.json"
        schema_file.write_text(json.dumps(REVIEW_SCHEMA), encoding="utf-8")
        prefix, config, binary = _sandbox_prefix("codex", cwd, home, ro_paths)
        argv = [
            str(binary),
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--cd",
            str(cwd),
            "--ephemeral",
            "--skip-git-repo-check",
            "-c",
            "mcp_servers={}",
            "--output-schema",
            f"{sandbox.SANDBOX_HOME}/schema.json",
            "--output-last-message",
            f"{sandbox.SANDBOX_HOME}/last.json",
            "-",
        ]
        done = _run(
            [*prefix, *argv], cwd=cwd, prompt=prompt,
            extra_env={},
            env_allowlist=(*config.env_allowlist, *CODEX_AUTH_ENV), timeout=timeout,
            family="codex",
        )
        message = last_file.read_text(encoding="utf-8") if last_file.exists() else ""
    if not message.strip():
        raise RunnerBadOutput("codex: no final message written", _tail(done.stdout + done.stderr))
    try:
        payload = _extract_json_object(message)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerBadOutput("codex: final message is not JSON", _tail(message)) from exc
    provenance, raw = _codex_provenance(done.stdout, done.stderr, message)
    banner_model = _CODEX_MODEL_RE.search(done.stderr)
    return replace(
        parse_review(payload, family="codex", head=head, kind=kind, raw=raw),
        model=banner_model.group(1) if banner_model else None,
        duration_s=done.duration_s,
        provenance=provenance,
    )


def review_with(
    family: str,
    *,
    cwd: Path,
    head: str,
    kind: str,
    diff: str,
    task_text: str,
    reviewer_ro_paths: tuple[str, ...] = (),
    timeout: int = 900,
) -> ReviewResult:
    """Run one review of ``diff`` at ``head`` with the given model family."""
    if family not in FAMILIES:
        raise ValueError(f"unknown reviewer family {family!r}; expected one of {FAMILIES}")
    if kind not in ("primary", "delta"):
        raise ValueError(f"unknown review kind {kind!r}; expected 'primary' or 'delta'")
    prompt = build_prompt(kind=kind, head=head, task_text=task_text, diff=diff)
    adapter = _review_claude if family == "claude" else _review_codex
    return adapter(
        prompt, cwd=cwd.resolve(), head=head, kind=kind, timeout=timeout,
        ro_paths=reviewer_ro_paths,
    )


def merge_reviews(results: list[ReviewResult]) -> ReviewResult:
    """Merge same-family chunk reviews while retaining every provider envelope/session."""
    if not results:
        raise ValueError("cannot merge no reviews")
    first = results[0]
    if any((r.family, r.head, r.kind) != (first.family, first.head, first.kind) for r in results):
        raise ValueError("chunk reviews do not describe the same review")
    findings = tuple(finding for result in results for finding in result.findings)
    verdict = "request_changes" if any(r.verdict == "request_changes" for r in results) else "approve"
    envelopes = [json.loads(result.raw) for result in results]
    sessions = [str(result.provenance["session_id"]) for result in results]
    provenance: dict[str, object] = {
        "family": first.family,
        "session_id": sessions[0],
        "session_ids": sessions,
    }
    if first.family == "claude":
        provenance.update(
            usage=[result.provenance["usage"] for result in results],
            total_cost_usd=sum(float(result.provenance["total_cost_usd"]) for result in results),
            duration_api_ms=sum(float(result.provenance["duration_api_ms"]) for result in results),
        )
    else:
        provenance["token_usage"] = [result.provenance["token_usage"] for result in results]
    return ReviewResult(
        family=first.family,
        head=first.head,
        kind=first.kind,
        verdict=verdict,
        findings=findings,
        raw=json.dumps(envelopes, separators=(",", ":")),
        model=", ".join(dict.fromkeys(r.model for r in results if r.model)) or None,
        duration_s=sum(r.duration_s for r in results if r.duration_s is not None),
        provenance=provenance,
        chunk_count=len(results),
    )


def split_diff(diff: str, budget: int) -> list[str]:
    """Group unified-diff file blocks below ``budget`` bytes."""
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", diff)]
    blocks = [
        diff[starts[i] : starts[i + 1] if i + 1 < len(starts) else len(diff)]
        for i in range(len(starts))
    ] if starts else [diff]
    if len(diff.encode()) > budget:
        blocks = [_summarize_deleted(block) for block in blocks]
    pieces = [piece for block in blocks for piece in _split_bytes(block, budget)]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if current and len(candidate.encode()) > budget:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def _summarize_deleted(block: str) -> str:
    if "\ndeleted file mode " not in block and "\n+++ /dev/null" not in block:
        return block
    match = re.match(r"diff --git a/(.+?) b/(.+?)\n", block)
    path = match.group(2) if match else "(unknown file)"
    deleted = sum(
        1 for line in block.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return f"diff --git a/{path} b/{path}\n{path}: deleted, {deleted} lines\n"


def _split_bytes(text: str, budget: int) -> list[str]:
    if len(text.encode()) <= budget:
        return [text]
    pieces: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line.encode()) > budget:
            if current:
                pieces.append(current)
                current = ""
            while line:
                cut = min(len(line), budget)
                while len(line[:cut].encode()) > budget:
                    cut -= 1
                pieces.append(line[:cut])
                line = line[cut:]
        elif current and len((current + line).encode()) > budget:
            pieces.append(current)
            current = line
        else:
            current += line
    if current:
        pieces.append(current)
    return pieces


def validate_runner_result(result: ReviewResult) -> bool:
    """Return whether saved provenance agrees with the provider envelope(s)."""
    provenance = result.provenance
    if (
        not isinstance(provenance, dict)
        or provenance.get("family") != result.family
        or isinstance(result.chunk_count, bool)
        or not isinstance(result.chunk_count, int)
        or result.chunk_count <= 0
    ):
        return False
    sessions = provenance.get("session_ids")
    if (
        not isinstance(sessions, list)
        or len(sessions) != result.chunk_count
        or not all(isinstance(item, str) and item for item in sessions)
        or provenance.get("session_id") != sessions[0]
    ):
        return False
    try:
        decoded = json.loads(result.raw)
    except (json.JSONDecodeError, TypeError):
        return False
    envelopes = decoded if isinstance(decoded, list) else [decoded]
    if len(envelopes) != result.chunk_count or not all(isinstance(item, dict) for item in envelopes):
        return False
    if result.family == "claude":
        envelope_sessions = [item.get("session_id") for item in envelopes]
        usages = [item.get("usage") for item in envelopes]
        costs = [item.get("total_cost_usd") for item in envelopes]
        durations = [item.get("duration_api_ms") for item in envelopes]
        expected_usage: object = usages[0] if len(usages) == 1 else usages
        provenance_ok = (
            envelope_sessions == sessions
            and all(item.get("type") == "result" for item in envelopes)
            and all(item.get("subtype", "success") == "success" for item in envelopes)
            and all(not item.get("is_error") for item in envelopes)
            and all(isinstance(item, dict) for item in usages)
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in costs)
            and all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in durations
            )
            and provenance.get("usage") == expected_usage
            and provenance.get("total_cost_usd") == sum(costs)
            and provenance.get("duration_api_ms") == sum(durations)
        )
        payloads = [item.get("structured_output") for item in envelopes]
        for index, payload in enumerate(payloads):
            if payload is None:
                try:
                    payloads[index] = _extract_json_object(str(envelopes[index].get("result", "")))
                except (json.JSONDecodeError, ValueError):
                    return False
    elif result.family == "codex":
        envelope_sessions = [item.get("session_id") for item in envelopes]
        usages = [item.get("token_usage") for item in envelopes]
        expected_usage = usages[0] if len(usages) == 1 else usages
        provenance_ok = (
            envelope_sessions == sessions
            and all(item.get("type") == "codex_exec_result" for item in envelopes)
            and all(isinstance(item, dict) for item in usages)
            and provenance.get("token_usage") == expected_usage
        )
        try:
            payloads = [_extract_json_object(str(item["final_message"])) for item in envelopes]
        except (KeyError, json.JSONDecodeError, ValueError):
            return False
    else:
        return False
    if not provenance_ok:
        return False
    try:
        parsed = [
            parse_review(
                payload, family=result.family, head=result.head, kind=result.kind, raw=result.raw
            )
            for payload in payloads
        ]
    except RunnerBadOutput:
        return False
    findings = tuple(finding for item in parsed for finding in item.findings)
    verdict = "request_changes" if any(
        item.verdict == "request_changes" for item in parsed
    ) else "approve"
    return result.findings == findings and result.verdict == verdict


# --------------------------------------------------------------------------- family


def family_from_trailer(value: str) -> str | None:
    """Map a ``Co-Authored-By`` value to a reviewer family, if recognisable."""
    lowered = value.lower()
    if "claude" in lowered or "anthropic" in lowered:
        return "claude"
    if any(word in lowered for word in ("codex", "gpt", "openai")):
        return "codex"
    return None


def author_family(cwd: Path, head: str) -> str | None:
    """Return the model family named in ``head``'s ``Co-Authored-By`` trailers."""
    done = _proc.run(
        ["git", "log", "-1", "--format=%(trailers:key=Co-Authored-By,valueonly)", head],
        cwd=cwd,
        env_allowlist=_ALLOWLIST,
        timeout=30,
    )
    if done.exit_code != 0:
        raise RunnerBadOutput(f"git log {head} exited {done.exit_code}", _tail(done.stderr))
    for line in done.stdout.splitlines():
        family = family_from_trailer(line)
        if family:
            return family
    return None


def pick_reviewer(preferences: tuple[str, ...], author: str | None) -> str:
    """Pick the first preferred family that differs from ``author``."""
    if not preferences:
        raise ValueError("no reviewer preferences configured")
    for family in preferences:
        if family != author:
            return family
    raise ValueError(f"no independent reviewer configured for author family {author}")
