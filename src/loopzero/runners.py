"""Model review adapters: thin subprocess wrappers around ``claude -p`` and ``codex exec``.

Both adapters share one prompt builder and one JSON findings schema, and both
return a :class:`~loopzero.types.ReviewResult`. Nothing here retries.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from loopzero import _proc
from loopzero.types import Finding, LoopZeroError, ReviewResult

FAMILIES = ("claude", "codex")
SEVERITIES = ("critical", "important", "suggestion")
BLOCKING = ("critical", "important")
TAIL_CHARS = 2000

# Extra environment each CLI needs to authenticate; forwarded only if present.
CLAUDE_AUTH_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR")
CODEX_AUTH_ENV = ("OPENAI_API_KEY", "CODEX_HOME")

_AUTH_PATTERNS = (
    re.compile(r"not logged in", re.IGNORECASE),
    re.compile(r"please run /login", re.IGNORECASE),
    re.compile(r"please (?:run )?`?codex login`?", re.IGNORECASE),
    re.compile(r"invalid api key", re.IGNORECASE),
    re.compile(r"authentication[_ ]error", re.IGNORECASE),
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
    verdict = payload.get("verdict")
    items = payload.get("findings")
    if verdict not in ("approve", "request_changes") or not isinstance(items, list):
        raise RunnerBadOutput(f"{family}: review lacks verdict/findings", _tail(raw))
    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict) or item.get("severity") not in SEVERITIES:
            raise RunnerBadOutput(f"{family}: finding has bad severity", _tail(raw))
        path = item.get("path")
        line = item.get("line")
        if path is not None and not isinstance(path, str):
            raise RunnerBadOutput(f"{family}: finding path is not a string", _tail(raw))
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            raise RunnerBadOutput(f"{family}: finding line is not an integer", _tail(raw))
        findings.append(
            Finding(
                severity=item["severity"],
                path=path,
                line=line,
                title=str(item.get("title", "")).strip() or "(untitled)",
                body=str(item.get("body", "")).strip(),
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
    timeout: int,
    family: str,
) -> _proc.Completed:
    try:
        done = _proc.run(
            argv,
            cwd=cwd,
            env_allowlist=_ALLOWLIST,
            extra_env=extra_env,
            timeout=timeout,
            input=prompt,
        )
    except (_proc.ToolMissing, FileNotFoundError) as exc:
        raise RunnerMissing(f"{family}: {argv[0]!r} not found on PATH") from exc
    if done.exit_code != 0:
        combined = f"{done.stdout}\n{done.stderr}"
        if _looks_like_auth_failure(combined):
            raise RunnerAuthFailed(f"{family}: CLI is not authenticated\n{_tail(combined)}")
        raise RunnerBadOutput(f"{family}: exited {done.exit_code}", _tail(combined))
    return done


_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


def _auth_env(names: tuple[str, ...]) -> dict[str, str]:
    return {name: os.environ[name] for name in names if os.environ.get(name)}


# --------------------------------------------------------------------------- adapters


def _review_claude(prompt: str, *, cwd: Path, head: str, kind: str, timeout: int) -> ReviewResult:
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
    ]
    done = _run(
        argv, cwd=cwd, prompt=prompt, extra_env=_auth_env(CLAUDE_AUTH_ENV),
        timeout=timeout, family="claude",
    )
    raw = done.stdout
    try:
        envelope = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerBadOutput("claude: stdout is not JSON", _tail(raw)) from exc
    if not isinstance(envelope, dict):
        raise RunnerBadOutput("claude: result envelope is not an object", _tail(raw))
    if envelope.get("is_error") or envelope.get("subtype", "success") != "success":
        text = str(envelope.get("result", ""))
        if _looks_like_auth_failure(text):
            raise RunnerAuthFailed(f"claude: CLI is not authenticated\n{_tail(text)}")
        raise RunnerBadOutput(f"claude: {envelope.get('subtype', 'error')}", _tail(raw))
    payload = envelope.get("structured_output")
    if payload is None:
        try:
            payload = _extract_json_object(str(envelope.get("result", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            raise RunnerBadOutput("claude: result text is not JSON", _tail(raw)) from exc
    return parse_review(payload, family="claude", head=head, kind=kind, raw=raw)


def _review_codex(prompt: str, *, cwd: Path, head: str, kind: str, timeout: int) -> ReviewResult:
    with tempfile.TemporaryDirectory(prefix="loopzero-codex-") as tmp:
        schema_file = Path(tmp, "schema.json")
        last_file = Path(tmp, "last.json")
        schema_file.write_text(json.dumps(REVIEW_SCHEMA), encoding="utf-8")
        argv = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--cd",
            str(cwd),
            "--ephemeral",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_file),
            "--output-last-message",
            str(last_file),
            "-",
        ]
        done = _run(
            argv, cwd=cwd, prompt=prompt, extra_env=_auth_env(CODEX_AUTH_ENV),
            timeout=timeout, family="codex",
        )
        raw = last_file.read_text(encoding="utf-8") if last_file.exists() else ""
    if not raw.strip():
        raise RunnerBadOutput("codex: no final message written", _tail(done.stdout + done.stderr))
    try:
        payload = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerBadOutput("codex: final message is not JSON", _tail(raw)) from exc
    return parse_review(payload, family="codex", head=head, kind=kind, raw=raw)


def review_with(
    family: str,
    *,
    cwd: Path,
    head: str,
    kind: str,
    diff: str,
    task_text: str,
    timeout: int = 900,
) -> ReviewResult:
    """Run one review of ``diff`` at ``head`` with the given model family."""
    if family not in FAMILIES:
        raise ValueError(f"unknown reviewer family {family!r}; expected one of {FAMILIES}")
    if kind not in ("primary", "delta"):
        raise ValueError(f"unknown review kind {kind!r}; expected 'primary' or 'delta'")
    prompt = build_prompt(kind=kind, head=head, task_text=task_text, diff=diff)
    adapter = _review_claude if family == "claude" else _review_codex
    return adapter(prompt, cwd=cwd, head=head, kind=kind, timeout=timeout)


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
    return preferences[0]
