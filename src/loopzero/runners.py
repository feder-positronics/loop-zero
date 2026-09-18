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
        if _looks_like_auth_failure(combined):
            raise RunnerAuthFailed(f"{family}: CLI is not authenticated\n{_tail(combined)}")
        raise RunnerBadOutput(f"{family}: exited {done.exit_code}", _tail(combined))
    return done


_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


def _review_config(family: str, binary: Path, ro_paths: tuple[str, ...]) -> Config:
    """Build a networked, read-only sandbox config independent of check settings."""
    auth = CLAUDE_AUTH_ENV if family == "claude" else CODEX_AUTH_ENV
    return Config(
        repo="review/sandbox",
        base_branch="main",
        checks=(),
        required_ci=(),
        merge_strategy="squash",
        reviewers=(family,),
        network=True,
        env_allowlist=(*BASE_ENV, *auth),
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
    prefix = sandbox.bwrap_argv(config, cwd, home, common_dir, git_dir)
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
            env_allowlist=config.env_allowlist, timeout=timeout, family="claude",
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
    usage = envelope.get("modelUsage")
    models = tuple(key for key in usage if isinstance(key, str)) if isinstance(usage, dict) else ()
    model = ", ".join(models) or None
    return replace(
        parse_review(payload, family="claude", head=head, kind=kind, raw=raw),
        model=model, duration_s=done.duration_s,
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
            env_allowlist=config.env_allowlist, timeout=timeout, family="codex",
        )
        raw = last_file.read_text(encoding="utf-8") if last_file.exists() else ""
    if not raw.strip():
        raise RunnerBadOutput("codex: no final message written", _tail(done.stdout + done.stderr))
    try:
        payload = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerBadOutput("codex: final message is not JSON", _tail(raw)) from exc
    banner_model = _CODEX_MODEL_RE.search(done.stderr)
    return replace(
        parse_review(payload, family="codex", head=head, kind=kind, raw=raw),
        model=banner_model.group(1) if banner_model else None, duration_s=done.duration_s,
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
