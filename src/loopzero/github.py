"""GitHub operations through the `gh` CLI: pull requests, reviews, checks, merge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from loopzero import runners
from loopzero._proc import ProcTimeout, ToolMissing, run
from loopzero.runners import review_settings
from loopzero.types import BLOCKING, Finding, ReviewResult

GH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")
APP_TOKEN_HELPER = Path.home() / ".config/loopzero/app-token"
_app_token_cache = ("", 0.0)
MISSING_REVIEW_REASON = "no review recorded for the current head"
MARKER_RE = re.compile(
    r"<!-- loopzero:finding v=1 severity=(?P<severity>\w+) "
    r"head=(?P<head>[0-9a-f]{40}) id=(?P<id>[0-9a-f]{8}) -->"
)
PAGE = 100
PR_FILE_LIMIT = 3_000
PR_APP_INSTALLATION_HINT = "Check that the GitHub App installation includes this repository."
RATE_LIMIT_GUIDANCE = "\nRate limited: retry after GitHub's reset or Retry-After; No automatic retry."
CHECK_SUITE_LIMIT = 1_000
GH_AUTH_REMEDY = "Run `gh auth login --scopes repo` (required token scope: repo)."
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_ANCHOR_NOTE = "No file location given by the reviewer; anchored here.\n\n"
_MOVED_NOTE = "Reviewer location {where} is not in the diff; anchored here.\n\n"
_SELF_REVIEW = re.compile(r"(approve|request changes on) your own pull request", re.IGNORECASE)
_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line
          comments(first: 1) {
            nodes { body author { __typename login } } pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
  }
}
"""


class GhError(Exception):
    def __init__(self, command: tuple[str, ...], tail: str, *,
                 token: str = "", guidance: str = "") -> None:
        self.command = tuple(_redact_tokens(arg, token) for arg in command)
        self.tail = _redact_tokens(tail, token) + guidance
        command_text = _redact_tokens(" ".join(command), token)
        super().__init__(f"{command_text} failed: {self.tail.strip()[-600:]}")


class GhUnavailable(GhError):
    """GitHub could not supply a result because of a transient fault or limit."""


class ReadDeadline(GhUnavailable):
    """A shared GitHub read deadline elapsed."""


class StatusLimitReached(GhError):
    """GitHub has no status slots left for this commit and context."""


class GhMissing(GhError):
    """The `gh` executable is not on PATH."""


class GhAuth(GhError):
    """`gh` is not authenticated."""


class MergeFailed(GhError):
    """Merge did not complete or could not be verified."""


@dataclass(frozen=True)
class PR:
    number: int
    url: str
    head_sha: str
    base_ref: str
    is_draft: bool
    state: str  # "OPEN" | "CLOSED" | "MERGED"
    mergeable: str  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
    author: str = ""  # login of the PR author
    merge_state: str = "UNKNOWN"  # GitHub mergeStateStatus, e.g. "BEHIND" | "CLEAN"
    body: str = ""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]
    waitable_failures: tuple[str, ...] = ()
    stale_verdicts: tuple[str, ...] = ()
    has_pending_checks: bool = False


TRANSIENT_RE = re.compile(r"HTTP 5\d\d|timed out|TLS handshake timeout|i/o timeout|"
                          r"connection reset|no server is currently|error connecting to api\.github\.com|"
                          r"unexpected end of JSON input|Something went wrong while executing your query", re.IGNORECASE)
RETRY_DELAY = 5.0
_retry_sleep = time.sleep
_read_scope: ContextVar[tuple[float, str | None, bool] | None] = ContextVar("github_reads", default=None)
_api_error_diagnostics: ContextVar[bool] = ContextVar("github_api_error_diagnostics", default=False)
_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})(?:\s+(.*))?$", re.IGNORECASE)
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]{10,})\b"
)


@contextmanager
def bounded_reads(deadline: float, token: str | None = "", *, retry: bool = False):
    """Share a monotonic budget across reads, including any retries."""
    previous = _read_scope.get()
    deadline = min(previous[0], deadline) if previous else deadline
    scope = _read_scope.set((deadline, token, retry))
    try:
        yield
        _read_timeout(0)
    finally:
        _read_scope.reset(scope)


def _read_timeout(limit: float, *, scoped: bool = True) -> float:
    scope = _read_scope.get() if scoped else None
    if scope is None:
        return limit
    remaining = scope[0] - time.monotonic()
    if remaining <= 0:
        raise ReadDeadline(("gh",), "GitHub read deadline exceeded")
    return min(limit, remaining)


@contextmanager
def api_error_diagnostics():
    """Include response headers and safe error details for a trusted API validation."""
    scope = _api_error_diagnostics.set(True)
    try:
        yield
    finally:
        _api_error_diagnostics.reset(scope)


def _included_response(output: str) -> tuple[str | None, dict[str, str], str]:
    """Split one `gh api --include` response into status, headers and body."""
    head, separator, body = output.partition("\r\n\r\n")
    line_separator = "\r\n"
    if not separator:
        head, separator, body = output.partition("\n\n")
        line_separator = "\n"
    if not separator:
        return None, {}, output
    lines = head.split(line_separator)
    match = _HTTP_STATUS_RE.match(lines[0])
    if match is None:
        return None, {}, output
    status = match.group(1)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().casefold()] = value.strip()
    return status, headers, body


def _api_method(args: tuple[str, ...]) -> str:
    for index, arg in enumerate(args[:-1]):
        if arg in ("--method", "-X"):
            return args[index + 1].upper()
    if {"-f", "-F", "--field", "--raw-field", "--input"} & set(args):
        return "POST"
    return "GET"


def _api_path(args: tuple[str, ...]) -> str:
    endpoint = args[1] if len(args) > 1 else ""
    path = endpoint.split("?", 1)[0]
    if "://" in path:
        path = path.partition("://")[2].partition("/")[2]
    return "/" + path.lstrip("/")


def redact_credentials(value: str) -> str:
    value = re.sub(r"""(?i)(https?://)[^\s/?#'"<>`]+@""", r"\1[REDACTED]@", value)
    value = re.sub(r"""(?i)(\bauthorization(?:\\?['"])?[ \t]*:[ \t]*)(\\?['"])(?:(?!\2)(?:\\.|[^\\\r\n]))+\2""",
                   r"\1\2[REDACTED]\2", value)
    return re.sub(r"""(?i)((?P<quote>['"])?\bauthorization[ \t]*:[ \t]*)"""
                  r"""(?(quote)(?:\\.|(?!(?P=quote))[^\\\r\n])+|(?:[a-z][\w-]*[ \t]+)?(?:\\.|[^\\\s'"`])+)""",
                  r"\1[REDACTED]", value)


def _redact_tokens(value: str, token: str = "") -> str:
    for secret in (token, os.environ.get("GH_TOKEN", ""),
                   os.environ.get("GITHUB_TOKEN", "")):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\b(authorization)\s*[:=]\s*"
                   r"(?:(?:basic|bearer|token)\s+)?[^\s,;]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    value = _GITHUB_TOKEN_RE.sub("[REDACTED]", value)
    value = re.sub(r"(?i)\b(access_token|auth_token|token)\s*[:=]\s*"
                   r"[^\s,;]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)(https?://[^?\s]+)\?[^\s]+", r"\1?[REDACTED]", value)
    return re.sub(r"(?<!\S)(/[^?\s]+)\?[^\s]+", r"\1?[REDACTED]", value)


def _api_error_detail(args: tuple[str, ...], stdout: str, stderr: str,
                      token: str, *, message_tail: str | None = None) -> str:
    status, headers, body = _included_response(stdout)
    if status is None:
        match = re.search(r"\bHTTP(?:/\S+)?\s+(\d{3})\b", stderr, re.IGNORECASE)
        status = match.group(1) if match else "unavailable"
    request_id = headers.get("x-github-request-id") or "unavailable"
    message = ""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        message_value = payload.get("message")
        if isinstance(message_value, str):
            message = message_value
        else:
            errors = payload.get("errors")
            if isinstance(errors, list):
                message = "; ".join(
                    error["message"] for error in errors
                    if isinstance(error, dict) and isinstance(error.get("message"), str)
                )
    if not message:
        message = body.strip() or stderr.strip().removeprefix("gh:").strip()
    message = " ".join(_redact_tokens(message if message_tail is None else message_tail, token).split())
    message = message[:400] or "no response message"
    return _redact_tokens(
        f"method={_api_method(args)} endpoint={_api_path(args)} status={status} "
        f"request_id={request_id} message={message}", token
    )


def _read_only(args: tuple[str, ...]) -> bool:
    """Only reads are retried: a repeated write could post or change state twice."""
    if args[:2] == ("api", "graphql"):
        return not any("mutation" in arg for arg in args)
    if args[:1] == ("api",):
        methods = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg in ("-X", "--method")]
        if methods:
            return all(method.upper() == "GET" for method in methods)
        return not {"-f", "-F", "--field", "--raw-field", "--input"} & set(args)
    return False


def _app_token_clock() -> float | None:
    """Read elapsed time including suspend, when the OS provides it."""
    if (clock := getattr(time, "CLOCK_BOOTTIME", None)) is not None:
        with suppress(OSError):
            return time.clock_gettime(clock)
    return None


def _cached_app_token() -> str:
    token, minted = _app_token_cache
    now = _app_token_clock() if token else None
    return token if now is not None and 0 <= now - minted < 3000 else ""


def app_token() -> str:
    global _app_token_cache
    if token := _cached_app_token():
        return token
    if not APP_TOKEN_HELPER.is_file() or not os.access(APP_TOKEN_HELPER, os.X_OK):
        return ""
    minted = _app_token_clock()  # before minting so a suspend mid-call ages the token
    try:
        result = run([str(APP_TOKEN_HELPER)], cwd=Path("/"), env_allowlist=GH_ENV, timeout=_read_timeout(10))
    except (ToolMissing, ProcTimeout, ReadDeadline, OSError):
        return ""
    if result.exit_code or not result.stdout.strip():
        return ""
    token = result.stdout.strip()
    if minted is None:
        return token  # no suspend-aware clock, so use this token only for this call
    _app_token_cache = (token, minted)
    return _cached_app_token()


def _select_token(token: str | None, scope: tuple[float, str | None, bool] | None, read_only: bool) -> str:
    selected = token if token is not None else (scope[1] if scope else None)
    return selected if selected is not None else (app_token() if read_only else "")


def _transient_error(tail: str, retry_idempotent: bool, status: str | None = None) -> bool:
    return (status or "").startswith("5") or bool(TRANSIENT_RE.search(tail)) or (
        retry_idempotent and "something went wrong while executing your query" in tail.lower()
    )


def _request_error(transient: bool) -> type[GhError]:
    return GhUnavailable if transient else GhError


def _rate_limited(status: str | None, headers: dict[str, str], tail: str) -> bool:
    return (status == "429"
            or (status == "403" and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers))
            or bool(re.search(r"rate limit|HTTP 429", tail, re.IGNORECASE)))


def _retry_pause(delay: float | None, limit: float, *, scoped: bool = True) -> bool:
    if delay is None or delay >= _read_timeout(limit, scoped=scoped):
        return False
    _retry_sleep(delay)
    return True


def _rate_limit_delay(headers: dict[str, str], delay: float | None) -> float | None:
    if delay is None or headers.get("x-ratelimit-remaining") == "0":
        return None
    value = headers.get("retry-after", "")
    try:
        wait = int(value) if re.fullmatch(r"[0-9]{1,18}", value) else parsedate_to_datetime(value).timestamp() - time.time()
    except (OverflowError, OSError, ValueError):
        return None
    return max(delay, wait)


def _pr_list_error_guidance(args: tuple[str, ...], stderr: str, using_app: bool) -> str:
    if (using_app and len(args) > 1 and args[0] == "api"
            and re.fullmatch(r"repos/[^/]+/[^/]+/pulls\?.+", args[1])
            and "(HTTP 404)" in stderr):
        return f"\n{PR_APP_INSTALLATION_HINT}"
    return ""


def _gh(*args: str, cwd: Path | None = None, timeout: float = 120,
        token: str | None = None, retry_idempotent: bool = False) -> str:
    diagnostic_api = _api_error_diagnostics.get() and args[:1] == ("api",)
    rest_api = args[:1] == ("api",) and args[:2] != ("api", "graphql")
    patch_error = args[:1] == ("api",) and _api_method(args) == "PATCH"
    request_args = args
    api = args[:1] == ("api",)
    if api and not {"--include", "-i"} & set(args):
        request_args = (*args, "--include")
    argv = ("gh", *request_args)
    cwd = cwd or Path("/")
    read_only = _read_only(args)
    retryable = read_only or retry_idempotent
    scope = _read_scope.get() if read_only else None
    delays = (RETRY_DELAY, None) if retryable and (not scope or scope[2]) else (None,)
    for delay in delays:
        selected = _select_token(token, scope, read_only)  # a retry backoff can outlive the app token
        try:
            done = run(list(argv), cwd=cwd, env_allowlist=GH_ENV,
                       extra_env={"GH_TOKEN": selected} if selected else None,
                       timeout=_read_timeout(timeout, scoped=read_only))
        except ToolMissing as exc:
            raise GhMissing(argv, str(exc), token=selected,
                            guidance=f"\nInstall GitHub CLI, then {GH_AUTH_REMEDY}") from exc
        except ProcTimeout as exc:
            _read_timeout(timeout, scoped=bool(scope and scope[2]))
            if not _retry_pause(delay, timeout, scoped=read_only):
                raise ProcTimeout(
                    _redact_tokens(str(exc), selected), _redact_tokens(exc.stdout, selected),
                    stderr=_redact_tokens(exc.stderr, selected), duration_s=exc.duration_s,
                ) from None
            continue
        status, headers, body = (_included_response(done.stdout) if api
                                 else (None, {}, done.stdout))
        tail = "\n".join(part for part in (done.stderr, body) if part)
        if done.exit_code and _rate_limited(status, headers, tail):
            if _retry_pause(_rate_limit_delay(headers, delay), timeout, scoped=read_only):
                continue
            guidance = (_redact_tokens(_rest_rate_limit(headers), selected) if rest_api or headers
                        else _graphql_reset(args, tail, cwd, selected) + RATE_LIMIT_GUIDANCE)
            if diagnostic_api or patch_error:
                detail = _api_error_detail(args, done.stdout if diagnostic_api else "", done.stderr, selected,
                                           message_tail=None if diagnostic_api else tail)
                raise GhUnavailable(("GitHub API request",), detail, token=selected, guidance=guidance)
            raise GhUnavailable(("gh", *args), tail, token=selected, guidance=guidance)
        transient = _transient_error(tail, retry_idempotent, status)
        if done.exit_code == 0 or not transient or not _retry_pause(delay, timeout, scoped=read_only):
            break
    if done.exit_code != 0:
        error_command = ("GitHub API request",) if diagnostic_api or patch_error else argv
        error_tail = (_api_error_detail(args, done.stdout if diagnostic_api else "", done.stderr, selected,
                                        message_tail=None if diagnostic_api else tail)
                      if diagnostic_api or patch_error else tail)
        if "gh auth login" in tail or "not logged in" in tail.lower():
            raise GhAuth(error_command, error_tail.rstrip(), token=selected, guidance=f"\n{GH_AUTH_REMEDY}")
        guidance = _pr_list_error_guidance(args, done.stderr, bool(read_only and selected and token is None))
        raise _request_error(transient)(error_command, error_tail, token=selected, guidance=guidance)
    return body


def _rest_rate_limit(headers: dict[str, str]) -> str:
    resource = headers.get("x-ratelimit-resource", "")
    remaining_value = headers.get("x-ratelimit-remaining", "")
    remaining = int(remaining_value) if re.fullmatch(r"[0-9]{1,18}", remaining_value) else None
    retry_after = headers.get("retry-after", "")
    retry_seconds = re.fullmatch(r"[0-9]{1,18}", retry_after)
    retry_date = re.fullmatch(
        r"[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT", retry_after,
    )
    reset = ""
    try:
        if epoch := headers.get("x-ratelimit-reset"):
            reset = datetime.fromtimestamp(int(epoch), UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        pass
    evidence = []
    if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", resource):
        evidence.append(f"X-RateLimit-Resource: {resource}")
    if remaining is not None:
        evidence.append(f"X-RateLimit-Remaining: {remaining}")
    if reset:
        evidence.append(f"X-RateLimit-Reset: {reset}")
    if retry_seconds or retry_date:
        evidence.append(f"Retry-After: {retry_after}")
    guidance = RATE_LIMIT_GUIDANCE
    if remaining == 0:
        guidance = f"\nRate limited: hourly quota exhausted: retry after {reset or 'the quota resets'}; No automatic retry."
    elif remaining is not None or retry_seconds or retry_date:
        delay = f"{retry_after} s" if retry_seconds else retry_after if retry_date else "~60 s"
        guidance = f"\nRate limited: short-window limit: retry after {delay}; No automatic retry."
    return "".join(f"\n{line}" for line in evidence) + guidance


def _graphql_reset(args: tuple[str, ...], tail: str, cwd: Path, token: str = "") -> str:
    """Best-effort reset evidence for a primary GraphQL limit, never a preflight."""
    if re.search(r"secondary|HTTP 429", tail, re.IGNORECASE):
        return ""
    graphql = args[:2] == ("api", "graphql") or (
        args[:1] == ("pr",) and "graphql" in tail.casefold())
    if not graphql or not re.search(r"rate limit (?:already )?exceeded", tail, re.IGNORECASE):
        return ""
    resource = re.search(r"(?im)^x-ratelimit-resource:\s*(\S+)", tail)
    remaining = re.search(r"(?im)^x-ratelimit-remaining:\s*(\d+)", tail)
    reset = re.search(r"(?im)^x-ratelimit-reset:\s*(\d+)", tail)
    if resource and resource[1].casefold() == "graphql" and remaining and remaining[1] == "0" and reset:
        try:
            stamp = datetime.fromtimestamp(int(reset[1]), UTC)
            if stamp.timestamp() > time.time():
                return f"\nGraphQL quota resets at {stamp.isoformat()} (response header)."
        except (OverflowError, OSError, ValueError):
            pass
    try:
        query = "query { rateLimit { remaining resetAt } }"
        result = run(["gh", "api", "graphql", "-f", f"query={query}"], cwd=cwd,
                     env_allowlist=GH_ENV, extra_env={"GH_TOKEN": token} if token else None,
                     timeout=_read_timeout(10))
        if result.exit_code:
            return ""
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get("errors"):
            return ""
        limit = data["data"]["rateLimit"]
        if not isinstance(limit, dict) or not isinstance(limit.get("resetAt"), str):
            return ""
        stamp = datetime.fromisoformat(limit["resetAt"])
        if (type(limit["remaining"]) is int and limit["remaining"] == 0
                and stamp.tzinfo is not None and stamp.timestamp() > time.time()):
            return f"\nGraphQL quota resets at {stamp.isoformat()} (diagnostic query)."
    except (ToolMissing, ProcTimeout, ReadDeadline, KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    return ""


def _gh_json(*args: str, token: str | None = None) -> object:
    out = _gh(*args, token=token)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as exc:
        raise GhError(("gh", *args), f"invalid JSON from gh at line {exc.lineno}, column {exc.colno}",
                      token=token or "") from None


def record_full_page(endpoint: str, rows: list, seen: set[bytes]) -> None:
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
    if digest in seen:
        raise GhError(("gh", "api", endpoint), "GitHub repeated a full page")
    seen.add(digest)


def _iter_paged(endpoint: str, key: str = "check_runs") -> Iterator[dict]:
    """Yield validated rows until the last page."""
    page = 1
    seen: set[bytes] = set()
    while True:
        chunk = _gh_json("api", endpoint, "--method", "GET", "-F", f"per_page={PAGE}",
                         "-F", f"page={page}")
        if isinstance(chunk, dict):
            chunk = chunk.get(key)
        if not isinstance(chunk, list) or not all(isinstance(row, dict) for row in chunk):
            raise GhError(("gh", "api", endpoint), "invalid paginated response")
        if len(chunk) >= PAGE:
            record_full_page(endpoint, chunk, seen)
        yield from chunk
        if len(chunk) < PAGE:
            return
        page += 1


def _paged(endpoint: str, key: str = "check_runs") -> list:
    return list(_iter_paged(endpoint, key))


def api_get(endpoint: str) -> object:
    """`gh api <endpoint>` parsed as JSON (None on an empty body)."""
    return _gh_json("api", endpoint)


def auth_token() -> str:
    """Token of the authenticated `gh` host, for Git transports that cannot call gh."""
    return _gh("auth", "token", "--hostname", "github.com").strip()


def login(token: str | None = None) -> str:
    """Login of the identity used for GitHub reads and review publishing."""
    data = _gh_json("api", "graphql", "-f", "query=query { viewer { login } }", token=token)
    content = data.get("data") if isinstance(data, dict) else None
    viewer = content.get("viewer") if isinstance(content, dict) else None
    name = viewer.get("login") if isinstance(viewer, dict) else None
    if not name:
        raise GhError(("gh", "api", "graphql"), "no login in viewer response")
    return str(name)


def _not_found(number: int, exc: Exception) -> GhError:
    return GhError(("gh", "api"), f"PR #{number} not found or not accessible ({exc!r})")


def _api(endpoint: str, payload: dict, method: str = "POST",
         token: str | None = None) -> object:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".loopzero", encoding="utf-8", delete_on_close=False
    ) as handle:
        handle.write(json.dumps(payload))
        handle.close()
        return _gh_json("api", endpoint, "--method", method, "--input", handle.name, token=token)


def merge_hold(number: int, labels: tuple[str, ...]) -> str | None:
    if "hold" in labels:
        return f"PR #{number} has a merge-authority hold; ask the decisions owner to release it"
    return None


def _pr_labels(data: dict) -> tuple[str, ...]:
    return tuple(label["name"] for label in data.get("labels", []))


def pr_from_rest(data: object) -> PR:
    """Normalize one REST pull detail without treating absent merge evidence as clean."""
    try:
        if not isinstance(data, dict):
            raise TypeError("PR detail is not an object")
        number = data["number"]
        head, base = data["head"], data["base"]
        url, head_sha, base_ref = data["html_url"], head["sha"], base["ref"]
        state, merged, draft = data["state"], data["merged"], data["draft"]
        mergeable = data["mergeable"]
        merge_state = data["mergeable_state"]
        if (type(number) is not int or number < 1 or state not in {"open", "closed"}
                or type(merged) is not bool or type(draft) is not bool
                or (mergeable is not None and type(mergeable) is not bool)
                or (merge_state is not None and not isinstance(merge_state, str))
                or (merged and state != "closed")
                or not isinstance(url, str) or not url.startswith("https://")
                or not isinstance(base_ref, str) or not base_ref
                or not isinstance(head_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", head_sha)):
            raise ValueError("invalid PR state, mergeability, or head")
        return PR(
            number=number, url=url, head_sha=head_sha,
            base_ref=base_ref, is_draft=draft,
            state="MERGED" if merged else state.upper(),
            mergeable="UNKNOWN" if mergeable is None else
                      ("MERGEABLE" if mergeable else "CONFLICTING"),
            merge_state=merge_state.upper() if merge_state else "UNKNOWN",
            author=str((data.get("user") or {}).get("login") or ""),
            body=str(data.get("body") or ""),
            labels=_pr_labels(data),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        number = data.get("number", "?") if isinstance(data, dict) else "?"
        raise _not_found(number, exc) from exc


def _pr_detail(repo: str, number: int) -> tuple[PR, dict]:
    data = api_get(f"repos/{repo}/pulls/{number}")
    pr = pr_from_rest(data)
    if pr.number != number:
        raise GhError(("gh", "api", f"repos/{repo}/pulls/{number}"),
                      f"PR detail number {pr.number} differs from requested {number}")
    return pr, data


def _matches_branch(source: dict, repo: str, branch: str) -> bool:
    source_repo = source.get("repo")
    name = source_repo.get("full_name") if isinstance(source_repo, dict) else None
    return source.get("ref") == branch and isinstance(name, str) and name.casefold() == repo.casefold()


def _branch_head(repo: str, branch: str) -> str | None:
    endpoint = f"repos/{repo}/branches/{quote(branch, safe='')}"
    try:
        ref = api_get(endpoint)
    except GhError as exc:
        if re.search(r"\bHTTP 404\b", exc.tail):
            return None
        raise
    commit = ref.get("commit") if isinstance(ref, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GhError(("gh", "api", endpoint), "invalid remote branch SHA")

    return sha


def _commit_branch_prs(repo: str, branch: str) -> tuple[str, list[dict]]:
    sha = _branch_head(repo, branch)
    if sha is None:
        return "", []

    matches: list[dict] = []
    for page in range(1, 101):
        endpoint = f"repos/{repo}/commits/{sha}/pulls?per_page={PAGE}&page={page}"
        rows = api_get(endpoint)
        if not isinstance(rows, list) or len(rows) > PAGE:
            raise GhError(("gh", "api", endpoint), "invalid commit PR page")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("head"), dict):
                raise GhError(("gh", "api", endpoint), "invalid commit PR head")
            if _matches_branch(row["head"], repo, branch):
                if (row.get("state") not in ("open", "closed")
                        or type(row.get("number")) is not int or row["number"] < 1
                        or not isinstance(row.get("created_at"), str)):
                    raise GhError(("gh", "api", endpoint), "invalid commit PR")
                matches.append(row)
        if len(rows) < PAGE:
            return endpoint, matches
    raise GhError(("gh", "api", endpoint), "commit PRs exceed 100 pages")


def _confirm_branch_pr(repo: str, branch: str, endpoint: str, number: int, state: str) -> PR:
    pr, detail = _pr_detail(repo, number)
    state_matches = pr.state == state or (state == "CLOSED" and pr.state == "MERGED")
    if not state_matches or not _matches_branch(detail["head"], repo, branch):
        raise GhError(("gh", "api", endpoint), "PR changed during branch lookup")
    return pr


def pr_for_branch(repo: str, branch: str, *, open_only: bool = False) -> PR | None:
    """Return the open PR, falling back to the most recent closed PR unless open_only."""
    endpoint, matches = _commit_branch_prs(repo, branch)
    open_matches = [row for row in matches if row["state"] == "open"]
    if open_matches:
        newest = max(open_matches, key=lambda row: (row["created_at"], row["number"]))
        return _confirm_branch_pr(repo, branch, endpoint, newest["number"], "OPEN")

    # A closed association cannot rule out an open PR or newer closed history.
    owner = repo.split("/", 1)[0]
    head = quote(f"{owner}:{branch}", safe="")
    for state in (("open",) if open_only else ("open", "closed")):
        endpoint = (f"repos/{repo}/pulls?state={state}&head={head}"
                    "&sort=created&direction=desc&per_page=1")
        rows = api_get(endpoint)
        if not isinstance(rows, list):
            raise GhError(("gh", "api", endpoint), "invalid PR list response")
        if rows:
            number = rows[0].get("number") if isinstance(rows[0], dict) else None
            if type(number) is not int or number < 1:
                raise GhError(("gh", "api", endpoint), "invalid PR number in list")
            return _confirm_branch_pr(repo, branch, endpoint, number, state.upper())
    return None


def _closed_history_number(repo: str, branch: str, endpoint: str, row: dict,
                           seen: set[int]) -> int:
    try:
        number, merged_at = row["number"], row["merged_at"]
        source = row["head"]
        if (type(number) is not int or number < 1 or number in seen
                or row["state"] != "closed" or not isinstance(source, dict)
                or not _matches_branch(source, repo, branch)):
            raise ValueError("invalid or repeated closed PR")
        if merged_at is not None and (
            not isinstance(merged_at, str) or datetime.fromisoformat(merged_at).tzinfo is None
        ):
            raise ValueError("invalid merged_at")
    except (KeyError, TypeError, ValueError) as exc:
        raise GhError(("gh", "api", endpoint), "invalid or repeated closed PR history") from exc
    return number


def merged_pr_for_branch(repo: str, branch: str) -> PR | None:
    """Prove completion, or read the complete closed history before publication."""
    head = quote(f"{repo.split('/', 1)[0]}:{branch}", safe="")
    commit_endpoint, matches = _commit_branch_prs(repo, branch)
    seen: set[int] = set()
    for page in range(101):
        if page == 0:
            endpoint = commit_endpoint
            rows = [row for row in matches if row["state"] == "closed"]
        else:
            endpoint = (f"repos/{repo}/pulls?state=closed&head={head}"
                        f"&sort=created&direction=desc&per_page={PAGE}&page={page}")
            rows = api_get(endpoint)
        if not isinstance(rows, list) or (page and len(rows) > PAGE):
            raise GhError(("gh", "api", endpoint), "invalid closed PR history page")
        for row in rows:
            number = _closed_history_number(repo, branch, endpoint, row, seen)
            seen.add(number)
            if row["merged_at"] is not None:
                pr, detail = _pr_detail(repo, number)
                if pr.state != "MERGED" or not _matches_branch(detail["head"], repo, branch):
                    raise GhError(("gh", "api", endpoint), "PR changed during completion lookup")
                return pr
        if page == 0:
            # A prior merge may belong to a different commit of this branch.
            seen.clear()
        elif len(rows) < PAGE:
            return None
    raise GhError(("gh", "api", endpoint), "closed PR history exceeds 100-page publication limit")


def pr_view(repo: str, number: int) -> PR:
    return _pr_detail(repo, number)[0]


def create_draft_pr(repo: str, branch: str, base: str, title: str, body: str) -> PR:
    detail = _api(
        f"repos/{repo}/pulls", {"head": branch, "base": base, "title": title,
                               "body": body, "draft": True})
    pr = pr_from_rest(detail)
    source_repo = detail["head"].get("repo") or {}
    if (pr.state != "OPEN" or not pr.is_draft or pr.base_ref != base
            or detail["head"].get("ref") != branch
            or not isinstance(source_repo, dict)
            or str(source_repo.get("full_name") or "").casefold() != repo.casefold()):
        raise GhError(("gh", "api", f"repos/{repo}/pulls"), "created PR differs from request")
    return pr


def update_body(repo: str, number: int, body: str) -> None:
    _api(f"repos/{repo}/pulls/{number}", {"body": body}, method="PATCH", token=app_token())


def finding_id(head_sha: str, path: str | None, line: int | None, title: str) -> str:
    raw = f"{head_sha}{path or ''}{line if line is not None else ''}{title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def finding_marker(severity: str, head_sha: str, finding: Finding) -> str:
    stable_id = finding_id(head_sha, finding.path, finding.line, finding.title)
    return (
        f"<!-- loopzero:finding v=1 severity={severity} head={head_sha} id={stable_id} -->"
    )


def _comment_body(f: Finding, head_sha: str, prefix: str = "") -> str:
    text = f"{prefix}**{f.severity}: {f.title}**\n\n{f.body}".rstrip()
    if f.severity in BLOCKING:
        return f"{finding_marker(f.severity, head_sha, f)}\n{text}"
    return text


ReviewSettingsLayout = Literal["current", "requested-reported", "summary"]


def _review_body(result: ReviewResult, unplaced: list[Finding], prefix: str = "", *,
                 settings_layout: ReviewSettingsLayout = "current", legacy_display: bool = False,
                 publication: tuple[str, int] | None = None) -> str:
    evidence = None
    if result.family == "codex":
        evidence = (runners.codex_evidence(result) if result.provenance
                    else runners._codex_evidence(result))
    if evidence is not None:
        if legacy_display or (publication and (evidence["repo"], evidence["pr"]) != publication):
            raise runners.RunnerBadOutput("codex: publication context mismatch")
        first, separator, rest = prefix.partition("\n")
        marker = runners.REVIEW_MARKER_RE.fullmatch(first)
        header = runners.encode_codex_evidence(evidence)
        if not marker or marker[1] != result.head or marker[2] != result.kind:
            raise runners.RunnerBadOutput("codex: invalid/oversized leading machine block")
        prefix = first + "\n" + header + (separator + rest if separator else "")
        _, span = runners.leading_review_block(prefix)
        if len(span.encode("utf-8")) > runners.CODEX_HEADER_LIMIT:
            raise runners.RunnerBadOutput("codex: oversized leading machine block; HOLD")
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in
              ("critical", "important", "suggestion")}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    duration = "unknown" if result.duration_s is None else f"{result.duration_s:.1f}s"
    settings = ""
    if settings_layout == "summary":
        effort = f" (effort {result.effort})" if result.effort else ""
        settings = f"model {result.model or 'unknown'}{effort}, "
    lines = [prefix.rstrip("\n")] if prefix else []
    lines += [
        (
            f"loopzero {result.kind} review by {result.family} on `{result.head}`, "
            f"{settings}{duration}: "
            f"**{result.verdict}** ({summary})"
        ),
    ]
    if result.fallback:
        lines.append(f"\nSame-family fallback: {result.fallback}. Cross-family review is preferred.")
    if result.chunk_count > 1:
        lines.append(f"\nReviewed in {result.chunk_count} chunks with the same model family.")
    if settings_layout != "summary":
        lines.extend(_review_settings_lines(result, include_configured=settings_layout == "current"))
    if unplaced:
        lines.append("\nFindings without a file location:\n")
        lines += [f"- **{f.severity}**: {f.title} — {f.body}".rstrip(" —") for f in unplaced]
    return "\n".join(lines) + _review_transcript(result, legacy_display)


def _review_settings_lines(result: ReviewResult, *, include_configured: bool) -> list[str]:
    claims = "launch configuration and CLI claims" if include_configured else "CLI claims"
    lines = [f"\nSettings per native call ({claims}, not backend attestation):"]
    for index, call in enumerate(review_settings(result), 1):
        request, reported = call["requested"], call["reported"]
        if result.family == "codex":
            request, reported = ({key: value if runners._identifier(value, 128 if key == "model" else 32) else None
                                  for key, value in settings.items()} for settings in (request, reported))
        lines.append(f"- Call {index}: requested model {request['model'] or 'unknown'} "
                     f"(effort {request['effort'] or 'unknown'}); "
                     f"CLI-reported model {reported['model'] or 'unknown'} "
                     f"(effort {reported['effort'] or 'unknown'})")
        if include_configured:
            configured = call["configured"]
            lines[-1] += (f"; configured model {configured['model'] or 'unknown'} "
                          f"(effort {configured['effort'] or 'unknown'})")
    return lines


def _review_transcript(result: ReviewResult, legacy_display: bool) -> str:
    if result.family == "claude" and not legacy_display:
        return "\n\nNative Claude output is retained in the restricted local review receipt."
    raw = result.raw
    if result.family == "codex" and not legacy_display:
        raw = ("Native Codex transcript retained in the restricted local review receipt; "
               "public evidence is publisher-derived, not backend attestation. "
               "Exact raw SHA256: " + hashlib.sha256(result.raw.encode()).hexdigest())
    encoded = raw.encode("utf-8")
    if len(encoded) > 30_000:
        raw = encoded[-30_000:].decode("utf-8", errors="ignore")
        raw = (
            "[Transcript truncated; showing the last at most 30,000 UTF-8 bytes. "
            "Full output remains in the saved local review artifact.]\n" + raw
        )
    return f"\n\n<details><summary>Raw reviewer output</summary>\n\n```\n{raw}\n```\n</details>"


def _right_lines(patch: str) -> tuple[set[int], int | None]:
    """Right-side (new file) line numbers present in `patch`, and the first added line."""
    lines: set[int] = set()
    first_added: int | None = None
    current = None
    for raw in patch.removesuffix("\n").split("\n"):
        hunk = _HUNK_RE.match(raw)
        if hunk:
            current = int(hunk.group(1))
            continue
        if current is None or raw.startswith(("-", "\\")):
            continue
        lines.add(current)
        if raw.startswith("+") and first_added is None:
            first_added = current
        current += 1
    return lines, first_added


def _require_complete_file_list(repo: str, number: int, head_sha: str, count: int, missing: str) -> None:
    pr, detail = _pr_detail(repo, number)
    request = ("gh", "api", f"repos/{repo}/pulls/{number}/files")
    if pr.head_sha != head_sha:
        raise GhError(request, f"PR #{number} head moved from {head_sha[:12]} to {pr.head_sha[:12]}; rerun review")
    if detail.get("changed_files") != count:
        raise GhError(request,
                      f"cannot place finding on PR #{number} at {missing!r}: "
                      f"GitHub's {PR_FILE_LIMIT:,}-file limit prevents verifying the complete file list")


def _diff_map(repo: str, number: int, head_sha: str,
              findings: tuple[Finding, ...]) -> tuple[dict[str, set[int]], dict | None]:
    blocking = tuple(f for f in findings if f.severity in BLOCKING)
    remaining = {f.path for f in findings if f.path and (f.line or f.severity in BLOCKING)}
    commentable: dict[str, set[int]] = {}
    anchor: dict | None = None
    rows = _iter_paged(f"repos/{repo}/pulls/{number}/files") if remaining or blocking else ()
    count = 0
    for entry in rows:
        count += 1
        path = entry["filename"]
        lines, first_added = _right_lines(entry.get("patch") or "")
        commentable[path] = lines
        if lines and anchor is None:
            line = first_added if first_added is not None else min(lines)
            anchor = {"path": path, "line": line, "side": "RIGHT"}
        remaining.discard(path)
        if not remaining and (not blocking or anchor is not None):
            return commentable, anchor
    missing = next((f.path for f in blocking if f.path in remaining), None)
    if missing is not None and count >= PR_FILE_LIMIT:
        _require_complete_file_list(repo, number, head_sha, len(commentable), missing)
    return commentable, anchor


def review_payload(repo: str, number: int, head_sha: str, result: ReviewResult,
                   body_prefix: str) -> dict:
    """Regenerate the publication content from the runner result and current PR diff."""
    return review_payloads(repo, number, head_sha, result, [(body_prefix, "current", False)])[0]


def review_payloads(repo: str, number: int, head_sha: str, result: ReviewResult,
                    formats: list[tuple[str, ReviewSettingsLayout, bool]]) -> list[dict]:
    """Place findings once for all exact body formats used in read-back."""
    commentable, anchor = _diff_map(repo, number, head_sha, result.findings)
    comments: list[dict] = []
    loose: list[Finding] = []
    for f in result.findings:
        if f.path and f.line and f.line in commentable.get(f.path, ()):
            comments.append({"path": f.path, "line": f.line, "side": "RIGHT",
                             "body": _comment_body(f, head_sha)})
        elif f.severity not in BLOCKING:
            loose.append(f)
        elif anchor is None:
            raise GhError(("gh", "api", f"repos/{repo}/pulls/{number}/files"),
                          f"cannot anchor '{f.title}': PR has no diff hunks")
        else:
            note = _MOVED_NOTE.format(where=f"{f.path}:{f.line}") if f.path else _ANCHOR_NOTE
            comments.append({**anchor, "body": _comment_body(f, head_sha, prefix=note)})
    return [{
        "commit_id": head_sha,
        "body": _review_body(result, loose, body_prefix, settings_layout=settings_layout,
                             legacy_display=legacy_display, publication=(repo, number)),
        "comments": comments,
    } for body_prefix, settings_layout, legacy_display in formats]


def post_review(
    repo: str,
    number: int,
    head_sha: str,
    result: ReviewResult,
    body_prefix: str = "",
    pr: PR | None = None,
    token: str | None = None,
    *,
    publisher: str | None = None,
) -> None:
    """Post `result` as a PR review on `head_sha`, one inline comment per finding.

    `body_prefix`, when given, becomes the first line of the review body. When the
    token user authored the PR (`pr` is fetched if not given) the review is posted
    as a COMMENT, since GitHub rejects self-approval and self-request-changes.

    Findings whose location is missing or outside the diff are anchored on the first
    changed file with a hunk, so every blocking finding creates a review thread.
    A supplied publisher is the login already checked for the supplied token.
    """
    author = (pr or pr_view(repo, number)).author
    token = app_token() if token is None else token  # "" means the operator identity
    if token or (author and author == (publisher or login(token=token))):
        event = "COMMENT"
    else:
        event = "APPROVE" if result.verdict == "approve" else "REQUEST_CHANGES"
    payload = {**review_payload(repo, number, head_sha, result, body_prefix), "event": event}
    endpoint = f"repos/{repo}/pulls/{number}/reviews"
    try:
        _api(endpoint, payload, token=token)
    except GhError as exc:
        if event == "COMMENT" or not _SELF_REVIEW.search(exc.tail):
            raise
        _api(endpoint, {**payload, "event": "COMMENT"}, token=token)


def _title_of(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    title_line = next((ln for ln in lines if ln.startswith("**")), lines[0] if lines else "")
    return re.sub(r"^\*\*\w+:\s*|\*\*$", "", title_line).strip()


def _thread_marker(node: dict, publishers: tuple[str, ...]) -> re.Match[str] | None:
    """The v=1 marker opening a thread's first comment, if a review publisher wrote it."""
    comments = node.get("comments", {}).get("nodes") or []
    if not comments:
        return None
    author = comments[0].get("author") or {}
    login = str(author.get("login") or "")
    # GraphQL names an App's bot account without REST's "[bot]" suffix.
    if author.get("__typename") == "Bot":
        login += "[bot]"
    if not login or login.casefold() not in {p.casefold() for p in publishers}:
        return None
    # The publisher writes the marker as the whole first line, exactly as finding_marker does.
    return MARKER_RE.fullmatch((comments[0].get("body") or "").partition("\n")[0])


def _parse_thread(node: dict, current_head: str | None,
                  publishers: tuple[str, ...]) -> Finding | None:
    match = _thread_marker(node, publishers)
    if not match or match.group("severity") not in BLOCKING:
        return None
    body = node["comments"]["nodes"][0]["body"]
    marker_head = match.group("head")
    if current_head is not None and marker_head != current_head and node.get("isOutdated"):
        return None
    severity = match.group("severity")
    rest = body[match.end():].lstrip()
    return Finding(severity=severity, path=node.get("path"), line=node.get("line"),
                   title=_title_of(rest), body=rest.strip())


_REPLY_MUTATION = (
    "mutation($thread:ID!,$body:String!){addPullRequestReviewThreadReply(input:"
    "{pullRequestReviewThreadId:$thread,body:$body}){comment{id}}}"
)
_RESOLVE_MUTATION = (
    "mutation($thread:ID!){resolveReviewThread(input:{threadId:$thread}){thread{isResolved}}}"
)


def open_findings(repo: str, number: int,
                  publishers: tuple[str, ...]) -> list[tuple[str, str, Finding]]:
    """(marker id, thread id, finding) for every unresolved blocking thread, any head."""
    return [
        (_thread_marker(node, publishers).group("id"), node["id"], f)
        for node in _unresolved_threads(repo, number)
        if (f := _parse_thread(node, None, publishers))
    ]


_THREAD_REPLIES_QUERY = """
query($thread: ID!, $after: String!) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $after) {
        nodes { body viewerDidAuthor }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _next_thread_cursor(page: dict, seen: set[str]) -> str:
    cursor = page.get("endCursor")
    if not isinstance(cursor, str) or not cursor.strip() or cursor in seen:
        raise GhError(("gh", "api", "graphql"), "missing or repeated review thread cursor")
    seen.add(cursor)
    return cursor


def _has_thread_reply(node: dict, reply: str) -> bool:
    page = node["comments"].get("pageInfo", {})
    seen: set[str] = set()
    while page.get("hasNextPage"):
        cursor = _next_thread_cursor(page, seen)
        data = _gh_json("api", "graphql", "-f", f"query={_THREAD_REPLIES_QUERY}",
                        "-F", f"thread={node['id']}", "-f", f"after={cursor}")
        try:
            comments = data["data"]["node"]["comments"]
            page = comments["pageInfo"]
            if any(comment.get("viewerDidAuthor") is True and comment.get("body") == reply
                   for comment in comments["nodes"]):
                return True
        except (KeyError, TypeError) as exc:
            raise GhError(("resolve", node["id"]), "could not read thread replies") from exc
    return False


def resolve_finding(repo: str, number: int, finding_id: str, reply: str,
                    publishers: tuple[str, ...]) -> Finding:
    """Reply to a blocking finding, or resume an identical resolution."""
    threads = _review_threads(repo, number, include_resolved=True)
    for node in sorted(threads, key=lambda node: bool(node.get("isResolved"))):
        finding = _parse_thread(node, None, publishers)
        if finding is None:
            continue
        thread_id = node["id"]
        if _thread_marker(node, publishers).group("id") != finding_id:
            continue
        confirmed = _has_thread_reply(node, reply)
        if node.get("isResolved"):
            if confirmed:
                return finding
            continue
        if not confirmed:
            # Two calls on purpose: fields of one mutation are not transactional, and a
            # thread must never be resolved unless its reply was confirmed.
            posted = _gh_json("api", "graphql", "-f", f"query={_REPLY_MUTATION}",
                              "-F", f"thread={thread_id}", "-f", f"body={reply}")
            try:
                posted["data"]["addPullRequestReviewThreadReply"]["comment"]["id"]
            except (KeyError, TypeError) as exc:
                raise GhError(("resolve", finding_id), "reply was not posted; not resolving") from exc
        try:
            response = _gh("api", "graphql", "-f", f"query={_RESOLVE_MUTATION}",
                           "-F", f"thread={thread_id}", retry_idempotent=True)
            thread = json.loads(response)["data"]["resolveReviewThread"]["thread"]
            if thread["isResolved"] is not True:
                raise ValueError("GitHub did not return isResolved=true")
        except (GhError, ProcTimeout, KeyError, TypeError, ValueError) as exc:
            raise GhError(
                ("resolve", finding_id),
                f"reply posted to finding {finding_id}, but thread resolution was not confirmed. "
                f"Retry loopzero resolve with the same reply on PR #{number}.\n{exc}",
            ) from exc
        return finding
    raise GhError(("resolve", finding_id), f"no open blocking finding {finding_id} on #{number}")


def eligibility_dispatch_args(repo: str, number: int, base: str) -> tuple[str, ...]:
    return ("api", "-X", "POST", f"repos/{repo}/actions/workflows/eligibility.yml/dispatches",
            "-f", f"ref={base}", "-f", f"inputs[pr]={number}")


def _eligibility_dispatched(repo: str, number: int, base: str, since: str) -> bool:
    query = f"event=workflow_dispatch&branch={quote(base, safe='')}&created={quote('>=' + since, safe='')}&per_page=100"
    data = api_get(f"repos/{repo}/actions/workflows/eligibility.yml/runs?{query}")
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    complete = isinstance(runs, list) and data.get("total_count") == len(runs)
    identified = [run.get("display_title") if isinstance(run, dict) else None for run in runs] if complete else []
    if f"Refresh eligibility for PR #{number}" in identified:
        return True
    if not complete or any(not isinstance(name, str) or not re.fullmatch(r"Refresh eligibility for PR #[0-9]+", name) for name in identified):
        raise GhError(("refresh Loop-zero Eligibility",), "could not identify all recent refresh runs")
    return False


def refresh_eligibility(repo: str, pr: PR, branch: str, base: str, publishers: tuple[str, ...]) -> None:
    args = eligibility_dispatch_args(repo, pr.number, base)
    since = datetime.fromtimestamp(time.time(), UTC).isoformat(timespec="seconds")
    try:
        try:
            _gh(*args)
        except (GhUnavailable, ProcTimeout) as exc:
            if isinstance(exc, GhUnavailable) and not _transient_error(exc.tail, False):
                raise
            _retry_sleep(RETRY_DELAY)
            current = pr_for_branch(repo, branch, open_only=True)
            if current is None or (current.number, current.head_sha, current.base_ref) != (
                pr.number, pr.head_sha, pr.base_ref,
            ):
                raise GhError(("refresh Loop-zero Eligibility",), "the PR or head changed after resolution") from exc
            if _eligibility_dispatched(repo, pr.number, base, since):
                return
            if open_blocking_findings(repo, pr.number, None, publishers):
                raise GhError(("refresh Loop-zero Eligibility",), "blocking findings appeared after resolution") from exc
            _gh(*args)
    except (GhError, ProcTimeout, OSError) as exc:
        retry = shlex.join(("gh", *args))
        raise GhError(("refresh Loop-zero Eligibility",),
                      f"Finding on PR #{pr.number} is resolved. {exc}\n"
                      f"Verify the PR head, remaining findings and recent runs before retrying: {retry}") from exc


def open_blocking_findings(
    repo: str, number: int, current_head: str | None, publishers: tuple[str, ...],
) -> list[Finding]:
    """Findings from unresolved review threads whose first comment opens with a blocking marker.

    Only critical and important v=1 markers on the first line of a comment written by a
    review publisher block. When `current_head` is supplied, an outdated thread is ignored
    unless its marker names that head.
    """
    return [
        f for node in _unresolved_threads(repo, number)
        if (f := _parse_thread(node, current_head, publishers))
    ]


def _unresolved_threads(repo: str, number: int) -> list[dict]:
    return _review_threads(repo, number)


def _review_threads(repo: str, number: int, *, include_resolved: bool = False) -> list[dict]:
    owner, name = repo.split("/", 1)
    after: str | None = None
    seen: set[str] = set()
    found: list[dict] = []
    while True:
        args = [
            "api", "graphql", "-f", f"query={_THREADS_QUERY}", "-F", f"owner={owner}",
            "-F", f"name={name}", "-F", f"number={number}",
        ]
        if after:
            args += ["-F", f"after={after}"]
        data = _gh_json(*args)
        try:
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = threads["nodes"]
            page = threads["pageInfo"]
            found += [node for node in nodes if include_resolved or not node.get("isResolved")]
            if not page["hasNextPage"]:
                return found
            after = _next_thread_cursor(page, seen)
        except (KeyError, TypeError) as exc:
            raise _not_found(number, exc) from exc


def _check_signals(
    repo: str,
    head_sha: str,
    review_at: str | None = None,
    review_grace: bool = False,
    required_ci: tuple[str, ...] = (),
    stale_verdict_grace: bool = False,
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...], bool]:
    signals: dict[str, list[tuple[str, str | None]]] = {}
    latest_runs: dict[str, dict] = {}
    runs = _paged(f"repos/{repo}/commits/{head_sha}/check-runs")
    for cr in runs:
        previous = latest_runs.get(cr["name"])
        if previous is None or _check_run_order(cr) > _check_run_order(previous):
            latest_runs[cr["name"]] = cr
    for name, cr in latest_runs.items():
        signals[name] = [
            (
                cr.get("conclusion") or cr.get("status") or "unknown",
                cr.get("completed_at") or cr.get("started_at"),
            )
        ]
    latest_status: dict[str, dict] = {}
    for status in _paged(f"repos/{repo}/commits/{head_sha}/statuses"):
        latest_status.setdefault(status["context"], status)
    for name, status in latest_status.items():
        signals.setdefault(name, []).append(
            (status.get("state") or "unknown", status.get("created_at"))
        )
    states = {
        name: _aggregate_signal([state for state, _ in values])
        for name, values in signals.items()
    }
    pending = {"pending", "queued", "in_progress", "requested", "waiting", "expected"}
    stale = tuple(name for name, values in signals.items() if review_at
                  and states[name] not in pending and states[name] != "success"
                  and all(at and at < review_at for state, at in values
                          if state != "success" and state not in pending)
                  and (review_grace or any(at and at >= review_at
                                           for state, at in values if state in pending)))
    stale_verdicts = tuple(
        name
        for name, values in signals.items()
        if review_at
        and stale_verdict_grace
        and name not in stale
        and states[name] in {"failure", "error", "cancelled", "timed_out"}
        and all(at and at < review_at for state, at in values if state != "success")
    )
    stale = (*stale, *stale_verdicts)
    superseded: list[str] = []
    workflows: list[dict] | None = None
    for name in required_ci:
        cr = latest_runs.get(name)
        if (name in stale or cr is None or states.get(name) in pending | {"success"}
                or cr.get("conclusion") not in {"failure", "cancelled", "timed_out"}
                or cr.get("status") != "completed" or not (cr.get("check_suite") or {}).get("id")
                or any(state not in pending | {"success"} for state, _ in signals[name][1:])):
            continue
        if workflows is None:
            workflows = _paged(f"repos/{repo}/actions/runs?head_sha={head_sha}", "workflow_runs")
        if _has_active_replacement(repo, cr, workflows, head_sha):
            superseded.append(name)
    capped = _check_suites_at_limit(runs)
    _discard_unverified_successes(states, latest_status.keys() - latest_runs.keys(), capped)
    return states, (*stale, *superseded), stale_verdicts, capped


def _discard_unverified_successes(states: dict[str, str], status_only: set[str], capped: bool) -> None:
    # A status-only success may hide a failed run outside the returned suites.
    if capped:
        for name in status_only:
            if states[name] == "success":
                del states[name]


def _check_suites_at_limit(runs: list[dict]) -> bool:
    suites = {
        suite["id"] for run in runs
        if isinstance(suite := run.get("check_suite"), dict) and type(suite.get("id")) is int
    }
    return len(suites) >= CHECK_SUITE_LIMIT


def _missing_check_reason(name: str, head_sha: str, capped: bool) -> str:
    if capped:
        return (f"cannot determine required check '{name}' on {head_sha[:12]}: "
                f"GitHub's {CHECK_SUITE_LIMIT:,}-suite limit")
    return f"required check '{name}' missing on {head_sha[:12]}"


def _has_active_replacement(repo: str, check: dict, workflows: list[dict], head_sha: str) -> bool:
    """A producer can be superseded only by its own workflow on the same head."""
    suite_id = check["check_suite"]["id"]
    def find_producer(runs: list[dict]) -> dict | None:
        return next((run for run in runs if run.get("check_suite_id") == suite_id
                     and run.get("head_sha") == head_sha), None)
    producer = find_producer(workflows)
    if producer is None and len(workflows) >= 1_000 and type(suite_id) is int:
        producer = find_producer(_paged(f"repos/{repo}/actions/runs?check_suite_id={suite_id}", "workflow_runs"))
    if producer is None or not producer.get("workflow_id"):
        return False
    created = producer.get("created_at")
    for workflow in workflows:
        if (workflow.get("head_sha") != head_sha
                or workflow.get("workflow_id") != producer["workflow_id"]
                or workflow.get("status") not in {"queued", "in_progress", "waiting", "pending", "requested"}):
            continue
        if workflow.get("id") == producer.get("id"):
            if (workflow.get("run_attempt", 1) > 1 and check.get("completed_at")
                    and workflow.get("run_started_at")
                    and workflow["run_started_at"] > check["completed_at"]):
                return True
        elif (created and isinstance(producer.get("id"), int)
              and workflow.get("created_at") and isinstance(workflow.get("id"), int)
              and (workflow["created_at"], workflow["id"]) > (created, producer["id"])):
            return True
    return False


def _check_run_order(run: dict) -> tuple[int, str]:
    run_id = run.get("id")
    started_at = run.get("started_at")
    return (run_id if isinstance(run_id, int) else -1,
            started_at if isinstance(started_at, str) else "")


def _aggregate_signal(states: list[str]) -> str:
    if all(state == "success" for state in states):
        return "success"
    pending = {"pending", "queued", "in_progress", "requested", "waiting", "expected"}
    failures = [state for state in states if state != "success" and state not in pending]
    return failures[0] if failures else "pending"


def behind_reason(pr: PR, reviewed_head: str | None) -> str:
    if reviewed_head != pr.head_sha:
        return f"PR #{pr.number} is behind {pr.base_ref}; rebase and rerun checks"
    return (
        f"PR #{pr.number} is behind {pr.base_ref}. Finish any pending delta review, then run "
        f"`git fetch origin {shlex.quote(pr.base_ref)}`, "
        f"`git merge {shlex.quote(f'origin/{pr.base_ref}')}`, `loopzero check` and `loopzero pr` before retrying readiness."
    )


def readiness(
    repo: str,
    pr: PR,
    base_branch: str,
    required_ci: tuple[str, ...],
    reviewed_head: str | None,
    *, allow_behind: bool = False, ancestor_review: bool = False,
    review_at: str | None = None,
    review_grace: bool = False,
    workflow_wait: bool = False,
    stale_verdict_grace: bool = False,
    publishers: tuple[str, ...],
) -> Readiness:
    """Decide whether `pr` may be marked ready / merged. Never raises on policy failures."""
    reasons: list[str] = []
    if pr.state != "OPEN":
        reasons.append(f"PR #{pr.number} is {pr.state}, not open")
    if pr.base_ref != base_branch:
        reasons.append(f"PR targets {pr.base_ref}, configured base is {base_branch}")
    if pr.mergeable == "CONFLICTING":
        reasons.append(f"PR #{pr.number} has merge conflicts with {pr.base_ref}")
    if pr.merge_state == "BEHIND" and not allow_behind:
        reasons.append(behind_reason(pr, reviewed_head))
    if not reviewed_head:
        reasons.append(MISSING_REVIEW_REASON)
    elif pr.head_sha != reviewed_head:
        reasons.append(f"head {pr.head_sha[:12]} differs from reviewed {reviewed_head[:12]}")
    for f in open_blocking_findings(repo, pr.number, None if ancestor_review else pr.head_sha,
                                    publishers):
        where = f"{f.path}:{f.line}" if f.path else "(no location)"
        reasons.append(f"open {f.severity} finding at {where}: {f.title}")
    checks, stale, stale_verdicts, capped = (_check_signals(
        repo, pr.head_sha, review_at, review_grace, required_ci if workflow_wait else (),
        workflow_wait and stale_verdict_grace,
    ) if required_ci else ({}, (), (), False))
    for name in required_ci:
        conclusion = checks.get(name)
        if conclusion is None:
            reasons.append(_missing_check_reason(name, pr.head_sha, capped))
        elif conclusion != "success":
            reasons.append(f"required check '{name}' is {conclusion}")
    return Readiness(
        ready=not reasons,
        reasons=tuple(reasons),
        waitable_failures=tuple(
            f"required check '{name}' is {checks[name]}"
            for name in required_ci
            if name in stale
            or (
                workflow_wait
                and review_grace
                and checks.get(name) in {"failure", "error", "cancelled", "timed_out"}
            )
        ),
        stale_verdicts=tuple(name for name in required_ci if name in stale_verdicts),
        has_pending_checks="pending" in checks.values(),
    )


def mark_ready(repo: str, number: int) -> None:
    _gh("pr", "ready", str(number), "--repo", repo)


_QUEUE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){isInMergeQueue autoMergeRequest{enabledAt}}}}"
)


class ClosedUnmerged(GhError):
    """A PR read as closed but not merged; right after a queue merge this can be transient."""


def merged_sha(
    repo: str, number: int, *, expected_head: str | None = None, check_hold: bool = False,
) -> str | None:
    """The verified merge commit, or None while open. Admission may check the hold label."""
    argv = ("gh", "api", f"repos/{repo}/pulls/{number}")
    view = api_get(argv[2])
    if not isinstance(view, dict) or type(view.get("merged")) is not bool:
        raise GhError(argv, "invalid PR merge evidence")
    if expected_head and (not isinstance(view.get("head"), dict)
                          or view["head"].get("sha") != expected_head):
        raise GhError(argv, "PR head changed before merge verification")
    if view["merged"] and view.get("state") == "closed":
        sha = view.get("merge_commit_sha")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    elif not view["merged"] and view.get("state") == "open":
        if check_hold and (hold := merge_hold(number, _pr_labels(view))):
            raise GhError(argv, hold)
        return None  # An open PR's merge_commit_sha is only a test merge.
    if not view["merged"] and view.get("state") == "closed":
        raise ClosedUnmerged(argv, "PR closed without a verified merge or invalid merge evidence")
    raise GhError(argv, "PR closed without a verified merge or invalid merge evidence")


def merge_pending(repo: str, number: int) -> bool:
    """True when GitHub holds the PR for merging: queued, or accepted as an auto-merge
    request that it moves into the queue shortly after (observed ~1 minute, #180)."""
    owner, name = repo.split("/", 1)
    argv = (
        "api", "graphql", "-f", f"query={_QUEUE_QUERY}", "-F", f"owner={owner}",
        "-F", f"name={name}", "-F", f"number={number}",
    )
    try:
        data = _gh_json(*argv)
        pr = data["data"]["repository"]["pullRequest"]
        queued = pr["isInMergeQueue"]
        auto = pr.get("autoMergeRequest")
        if type(queued) is not bool or (auto is not None and not isinstance(auto, dict)):
            raise ValueError("invalid queue fields")
    except (GhError, ProcTimeout, KeyError, TypeError, ValueError) as exc:
        raise MergeFailed(("gh", "api", "graphql"),
                          f"GitHub queue lookup for PR #{number}: queue status could not be verified. "
                          f"Check the PR's queue or merge state before retrying.\n{str(exc)[-300:]}") from exc
    return queued or bool(auto)


def merge(repo: str, number: int, strategy: str, head_sha: str) -> str | None:
    """Merge PR `number` at exactly `head_sha` and return the verified merge SHA.

    Returns None when the base branch uses a merge queue and GitHub accepted the PR
    into it: the merge will land later, and the caller must verify on a later run.
    """
    if strategy not in ("squash", "merge", "rebase", "queue"):
        raise MergeFailed(("gh", "pr", "merge"), f"unknown merge strategy: {strategy}")
    # A merge queue owns the method; gh rejects an explicit --squash/--merge/--rebase.
    method = () if strategy == "queue" else (f"--{strategy}",)
    argv = (
        "pr", "merge", str(number), "--repo", repo, *method, "--match-head-commit", head_sha,
    )
    try:
        _gh(*argv, timeout=300)
    except GhError as exc:
        raise MergeFailed(exc.command, exc.tail) from exc
    sha = merged_sha(repo, number, expected_head=head_sha)
    if sha:
        return sha
    queue_error = None
    try:
        if merge_pending(repo, number):
            return None
    except MergeFailed as exc:
        queue_error = exc
    # The queue may have landed the PR between the two reads; look once more.
    sha = merged_sha(repo, number, expected_head=head_sha)
    if sha:
        return sha
    if queue_error:
        raise queue_error
    raise MergeFailed(("gh", *argv), f"PR #{number} neither merged nor queued")


def _remote_branch_head(endpoint: str) -> str | None:
    try:
        current = api_get(endpoint)
    except GhError as exc:
        if re.search(r"\bHTTP 404\b", exc.tail):
            return None
        raise
    obj = current.get("object") if isinstance(current, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not sha:
        raise GhError(("gh", "api", endpoint), "missing remote ref SHA")
    return sha


def delete_remote_branch(repo: str, branch: str, expected_head: str) -> bool:
    """Return False if the remote ref moved, or True if deleted or absent."""
    endpoint = f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"
    current = _remote_branch_head(endpoint)
    if current != expected_head:
        return current is None
    ref = f"refs/heads/{branch}"
    argv = ("git", "-c", "credential.helper=!gh auth git-credential",
            "push", f"--force-with-lease={ref}:{expected_head}",
            f"https://github.com/{repo}.git", f":{ref}")
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="loopzero-cleanup-") as scratch:
        init = ("git", "init", "--bare", "--template=")
        done = run(init, cwd=scratch, env_allowlist=GH_ENV, extra_env=env, timeout=120)
        if done.exit_code != 0:
            raise GhError(init, done.stderr or done.stdout)
        done = run(argv, cwd=scratch, env_allowlist=GH_ENV, extra_env=env, timeout=120)
    if done.exit_code == 0:
        return True
    current = _remote_branch_head(endpoint)
    if current != expected_head:
        return current is None
    raise GhError(argv, done.stderr or done.stdout)


def commit_status(repo: str, head: str, context: str, state: str, description: str) -> None:
    """Publish a SHA-scoped status using the caller's narrowly scoped credential."""
    endpoint = f"repos/{repo}/statuses/{head}"
    try:
        _api(endpoint, {"state": state, "context": context, "description": description})
    except GhError as exc:
        validation_error = re.search(r"\bHTTP 422\b|^method=POST endpoint=\S+ status=422\b", exc.tail)
        if validation_error and _status_limit_reached(repo, head, context):
            raise StatusLimitReached(
                exc.command, f"{head} context {context!r} reached GitHub's 1,000-status limit. "
                "Push a fresh head and obtain review evidence for it before refreshing eligibility."
            ) from exc
        raise


def status_matches(repo: str, head: str, context: str, state: str, description: str) -> bool:
    statuses = _iter_paged(f"repos/{repo}/commits/{head}/statuses")
    latest = next((row for row in statuses if row.get("context") == context), {})
    creator = latest.get("creator")
    return (isinstance(creator, dict) and creator.get("login") == "github-actions[bot]"
            and (latest.get("state"), latest.get("description")) == (state, description))


def _status_limit_reached(repo: str, head: str, context: str) -> bool:
    # A validation error alone does not prove exhaustion.
    with suppress(GhError, ProcTimeout, OSError):
        return sum(
            str(status.get("context", "")).casefold() == context.casefold()
            for status in _paged(f"repos/{repo}/commits/{head}/statuses")
        ) >= 1_000
    return False
