"""GitHub operations through the `gh` CLI: pull requests, reviews, checks, merge."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from itertools import chain, islice, pairwise
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from loopzero import runners
from loopzero._proc import SECRET_PATTERNS, Completed, ProcTimeout, ToolMissing, redact, run, secret_forms
from loopzero.runners import review_settings
from loopzero.types import BLOCKING, Finding, LoopZeroError, ReviewResult, is_sha

GH_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GH_HOST")
GH_ENV = (*GH_BASE_ENV, "GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "XDG_CONFIG_HOME")
APP_TOKEN_HELPER = Path.home() / ".config/loopzero/app-token"
PR_LOCK_DIR = Path.home() / ".cache/loopzero/pr-locks"
_app_token_cache = ("", 0.0)
MISSING_REVIEW_REASON = "no review recorded for this head. Run `loopzero review`."
BODY_BLOCKERS = "<!-- loopzero:body-blockers -->\n"
MERGEABILITY_PENDING = "GitHub is computing mergeability"
MARKER_RE = re.compile(
    r"<!-- loopzero:finding v=1 severity=(?P<severity>\w+) "
    r"head=(?P<head>[0-9a-f]{40}) id=(?P<id>[0-9a-f]{8}) -->"
)
PAGE = 100
PR_FILE_LIMIT = 3_000
PR_APP_INSTALLATION_HINT = "Check that the GitHub App installation includes this repository."
RATE_LIMIT_GUIDANCE = "\nRate limited: retry after GitHub's reset or Retry-After."
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


class GhError(LoopZeroError):
    def __init__(self, command: tuple[str, ...], tail: str, *,
                 token: str = "", guidance: str = "", confirmed_rate_limit: bool = False,
                 rate_reset: int | None = None) -> None:
        self.command = tuple(_redact_tokens(arg, token) for arg in command)
        self.tail = _redact_tokens(tail, token) + guidance
        self.confirmed_rate_limit = confirmed_rate_limit
        self.rate_reset = rate_reset
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
    node_id: str = field(default="", compare=False)


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]
    waitable_failures: tuple[str, ...] = ()
    stale_verdicts: tuple[str, ...] = ()
    has_pending_checks: bool = False
    checks: dict[str, tuple[str, str]] = field(default_factory=dict)  # reason -> (check name, state)


TRANSIENT_RE = re.compile(r"HTTP 5\d\d|timed out|TLS handshake timeout|i/o timeout|"
                          r"connection reset|no server is currently|error connecting to api\.github\.com|"
                          r"unexpected end of JSON input|Something went wrong while executing your query", re.IGNORECASE)
RETRY_DELAY = 5.0
_retry_sleep = time.sleep
_read_scope: ContextVar[tuple[float, str | None, bool] | None] = ContextVar("github_reads", default=None)
_api_error_diagnostics: ContextVar[bool] = ContextVar("github_api_error_diagnostics", default=False)
_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})(?:\s+(.*))?$", re.IGNORECASE)
_READINESS_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.([0-9]+))?"
                                  r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")
_HTTP_DATE = re.compile(r"[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT")


@contextmanager
def bounded_reads(deadline: float, token: str | None = "", *, retry: bool = False, check_on_exit: bool = True):
    """Share a monotonic budget across reads, including any retries."""
    previous = _read_scope.get()
    deadline = min(previous[0], deadline) if previous else deadline
    scope = _read_scope.set((deadline, token, retry))
    try:
        yield
        if check_on_exit:
            read_timeout(0)
    finally:
        _read_scope.reset(scope)


def read_timeout(limit: float = math.inf, *, scoped: bool = True) -> float:
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
    for line_separator in ("\r\n", "\n"):
        head, separator, body = output.partition(line_separator * 2)
        if not separator:
            continue
        status_line, *header_lines = head.split(line_separator)
        if not (match := _HTTP_STATUS_RE.match(status_line)):
            break
        headers = {name.strip().casefold(): value.strip()
                   for name, colon, value in (line.partition(":") for line in header_lines) if colon}
        return match.group(1), headers, body
    return None, {}, output


def _api_methods(args: tuple[str, ...]) -> list[str]:
    return [method.upper() for flag, method in pairwise(args) if flag in ("--method", "-X")]


_API_BODY_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}


def _api_method(args: tuple[str, ...]) -> str:
    methods = _api_methods(args)
    return methods[0] if methods else "POST" if _API_BODY_FLAGS.intersection(args) else "GET"


def redact_credentials(value: str) -> str:
    value = re.sub(r"""(?i)(https?://)[^\s/?#'"<>`]+@""", r"\1[REDACTED]@", value)
    value = re.sub(r"""(?i)(\bauthorization(?:\\?['"])?[ \t]*:[ \t]*)(\\?['"])(?:(?!\2)(?:\\.|[^\\\r\n]))+\2""",
                   r"\1\2[REDACTED]\2", value)
    return re.sub(r"""(?i)((?P<quote>['"])?\bauthorization[ \t]*:[ \t]*)"""
                  r"""(?(quote)(?:\\.|(?!(?P=quote))[^\\\r\n])+|(?:[a-z][\w-]*[ \t]+)?(?:\\.|[^\\\s'"`])+)""",
                  r"\1[REDACTED]", value)


def _redact_tokens(value: str, token: str = "") -> str:
    value = redact(value, secret_forms((token, os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_TOKEN", ""))), *SECRET_PATTERNS)
    value = re.sub(r"(?i)\b(authorization)\s*[:=]\s*(?:(?:basic|bearer|token)\s+)?[^\s,;]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)\b(access_token|auth_token|token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)(https?://[^?\s]+)\?[^\s]+", r"\1?[REDACTED]", value)
    return re.sub(r"(?<!\S)(/[^?\s]+)\?[^\s]+", r"\1?[REDACTED]", value)


def _api_error_detail(args: tuple[str, ...], response: tuple[str | None, dict[str, str], str],
                      stderr: str, token: str, *, message_tail: str | None = None) -> str:
    status, headers, body = response
    if status is None:
        match = re.search(r"\bHTTP(?:/\S+)?\s+(\d{3})\b", stderr, re.IGNORECASE)
        status = match.group(1) if match else "unavailable"
    request_id = headers.get("x-github-request-id") or "unavailable"
    message = ""
    with suppress(TypeError, ValueError):
        match json.loads(body):
            case {"message": str(value)}:
                message = value
            case {"errors": list(errors)}:
                message = "; ".join(
                    error["message"] for error in errors
                    if isinstance(error, dict) and isinstance(error.get("message"), str)
                )
    message = (message_tail if message_tail is not None
               else message or body.strip() or stderr.strip().removeprefix("gh:").strip())
    message = " ".join(_redact_tokens(message, token).split())[:400] or "no response message"
    path = (args[1] if len(args) > 1 else "").split("?", 1)[0]
    if "://" in path:
        path = path.partition("://")[2].partition("/")[2]
    return _redact_tokens(
        f"method={_api_method(args)} endpoint=/{path.lstrip('/')} status={status} "
        f"request_id={request_id} message={message}", token
    )


def _read_only(args: tuple[str, ...]) -> bool:
    """Only reads are retried: a repeated write could post or change state twice."""
    if args[:2] == ("api", "graphql"):
        return not any("mutation" in arg for arg in args)
    if args[:1] != ("api",):
        return False
    methods = _api_methods(args)
    return all(method == "GET" for method in methods) if methods else not _API_BODY_FLAGS.intersection(args)


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


@contextmanager
def pr_lock(repo: str, number: int) -> Iterator[None]:
    """Serialize a GitHub write across local checkouts for this PR."""
    key = hashlib.sha256(f"{repo.casefold()}#{number}".encode()).hexdigest()
    PR_LOCK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(PR_LOCK_DIR / key, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "rb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise GhError(("lock",), f"PR lock is not a regular file: {PR_LOCK_DIR / key}")
        while True:
            with suppress(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if _read_scope.get() else 0))
                read_timeout(0)
                break
            time.sleep(read_timeout(0.1))
        yield


def app_token() -> str:
    global _app_token_cache
    if token := _cached_app_token():
        return token
    if not APP_TOKEN_HELPER.is_file() or not os.access(APP_TOKEN_HELPER, os.X_OK):
        return ""
    minted = _app_token_clock()  # before minting so a suspend mid-call ages the token
    try:
        result = run([str(APP_TOKEN_HELPER)], cwd=Path("/"), env_allowlist=GH_BASE_ENV, timeout=read_timeout(10))
    except (ToolMissing, ProcTimeout, ReadDeadline, OSError):
        return ""
    if result.exit_code or not (token := result.stdout.strip()):
        return ""
    if minted is None:
        return token  # no suspend-aware clock, so use this token only for this call
    _app_token_cache = (token, minted)
    return _cached_app_token()


def _select_token(token: str | None, scope: tuple[float, str | None, bool] | None, read_only: bool) -> str:
    selected = token if token is not None else (scope[1] if scope else None)
    return selected if selected is not None else (app_token() if read_only else "")


def _transient_error(tail: str, status: str | None = None) -> bool:
    return (status or "").startswith("5") or bool(TRANSIENT_RE.search(tail))


def _rate_limited(status: str | None, headers: dict[str, str], tail: str) -> bool:
    return (status == "429"
            or (status == "403" and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers))
            or bool(re.search(r"rate limit|HTTP 429", tail, re.IGNORECASE)))


def _retry_pause(delay: float | None, limit: float, *, scoped: bool = True) -> bool:
    if delay is None or delay >= read_timeout(limit, scoped=scoped):
        return False
    _retry_sleep(delay)
    return True


def retry_after(value: str) -> float | None:
    try:
        if re.fullmatch(r"[0-9]{1,18}", value):
            return float(value)
        date = parsedate_to_datetime(value)
        return max(0.0, date.timestamp() - time.time()) if date.tzinfo is not None else None
    except (OverflowError, OSError, ValueError):
        return None


def _rate_limit_delay(headers: dict[str, str], delay: float | None) -> float | None:
    if delay is None or headers.get("x-ratelimit-remaining") == "0":
        return None
    wait = retry_after(headers.get("retry-after", ""))
    return None if wait is None else max(delay, wait)


def _pr_list_error_guidance(args: tuple[str, ...], stderr: str, using_app: bool) -> str:
    if (using_app and len(args) > 1 and args[0] == "api"
            and re.fullmatch(r"repos/[^/]+/[^/]+/pulls\?.+", args[1])
            and "(HTTP 404)" in stderr):
        return f"\n{PR_APP_INSTALLATION_HINT}"
    return ""


_ETAG_CACHE_BYTES = 4_000_000
_etag_cache: dict[tuple[tuple[str, ...], int], tuple[str, str, int]] = {}


def _cache_credential(selected: str) -> str:
    """The credential a cache is filed under: the one passed, else a GH_TOKEN/GITHUB_TOKEN gh will use.
    Empty for gh's stored account, which can change mid-run and is never cached."""
    return selected or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def _remember_response(dates, used, selected, key, headers, body):
    dates[:] = [headers.get("date", "")]
    if used is not None:
        used[:] = [selected]
    _remember_etag(key, headers, body)


def _remember_etag(key: tuple[tuple[str, ...], int] | None, headers: dict[str, str], body: str) -> None:
    if key and (etag := headers.get("etag")):
        _etag_cache.pop(key, None)
        size = len(body.encode())
        if size > _ETAG_CACHE_BYTES:
            return
        _etag_cache[key] = (etag, body, size)
        while sum(held[2] for held in _etag_cache.values()) > _ETAG_CACHE_BYTES:
            del _etag_cache[next(iter(_etag_cache))]


def _gh(*args: str, cwd: Path | None = None, timeout: float = 120,
        token: str | None = None, retry_idempotent: bool = False, dates: list[str] | None = None,
        conditional: bool = False, used: list[str] | None = None) -> str:
    """Run gh; `conditional` revalidates a repeated REST GET by ETag, since a 304 is free of the core quota."""
    dates = [] if dates is None else dates
    api = args[:1] == ("api",)
    diagnostic_api = api and _api_error_diagnostics.get()
    rest_api = api and args[:2] != ("api", "graphql")
    patch_error = api and _api_method(args) == "PATCH"
    command = ("gh", *args)
    argv = (*command, "--include") if api and not {"--include", "-i"} & set(args) else command
    cwd = cwd or Path("/")
    read_only = _read_only(args)
    retryable = read_only or retry_idempotent
    scope = _read_scope.get() if read_only else None
    delays = (RETRY_DELAY, None) if retryable and (not scope or scope[2]) else (None,)
    for delay in delays:
        selected = _select_token(token, scope, read_only)  # a retry backoff can outlive the app token
        credential = _cache_credential(selected)
        key = (args, hash(credential)) if credential and conditional and rest_api and read_only else None
        cached = _etag_cache.get(key)
        request = [*argv, "-H", f"If-None-Match: {cached[0]}"] if cached else list(argv)
        try:
            done = run(request, cwd=cwd, env_allowlist=GH_ENV,
                       extra_env={"GH_TOKEN": selected} if selected else None,
                       timeout=read_timeout(timeout, scoped=read_only), encoding="utf-8")
        except ToolMissing as exc:
            raise GhMissing(argv, str(exc), token=selected,
                            guidance=f"\nInstall GitHub CLI, then {GH_AUTH_REMEDY}") from exc
        except ProcTimeout as exc:
            read_timeout(timeout, scoped=bool(scope and scope[2]))
            if not _retry_pause(delay, timeout, scoped=read_only):
                raise ProcTimeout(
                    _redact_tokens(str(exc), selected), _redact_tokens(exc.stdout, selected),
                    stderr=_redact_tokens(exc.stderr, selected), duration_s=exc.duration_s,
                ) from None
            continue
        status, headers, body = response = _included_response(done.stdout) if api else (None, {}, done.stdout)
        tail = "\n".join(part for part in (done.stderr, body) if part)
        settled = cached[1] if cached and status == "304" else body if done.exit_code == 0 else None
        if settled is not None:
            _remember_response(dates, used, selected, key, headers, settled)
            return settled
        rate_limited = _rate_limited(status, headers, tail)
        transient = rate_limited or _transient_error(tail, status)
        pause = _rate_limit_delay(headers, delay) if rate_limited else delay
        if not transient or not _retry_pause(pause, timeout, scoped=read_only):
            break

    error_command, detail = (command if rate_limited else argv), tail
    if diagnostic_api or patch_error:
        error_command = ("GitHub API request",)
        detail = _api_error_detail(args, response if diagnostic_api else (None, {}, ""),
                                   done.stderr, selected, message_tail=None if diagnostic_api else tail)
    if rate_limited:
        guidance = (_redact_tokens(_rest_rate_limit(headers), selected) if rest_api or headers
                    else _graphql_reset(args, tail, cwd, selected) + RATE_LIMIT_GUIDANCE)
    else:
        if "gh auth login" in tail or "not logged in" in tail.lower():
            raise GhAuth(error_command, detail.rstrip(), token=selected, guidance=f"\n{GH_AUTH_REMEDY}")
        guidance = _pr_list_error_guidance(args, done.stderr, bool(read_only and selected and token is None))
    error = GhUnavailable if transient else GhError
    confirmed_limit = rate_limited and status in {"200", "403", "429"}
    reset = headers.get("x-ratelimit-reset", "")
    rate_reset = int(reset) if (confirmed_limit and headers.get("x-ratelimit-remaining") == "0"
                                and re.fullmatch(r"[0-9]{1,18}", reset)) else None
    raise error(error_command, detail, token=selected, guidance=guidance,
                confirmed_rate_limit=confirmed_limit, rate_reset=rate_reset)


def _rest_rate_limit(headers: dict[str, str]) -> str:
    resource = headers.get("x-ratelimit-resource", "")
    remaining_value = headers.get("x-ratelimit-remaining", "")
    remaining = int(remaining_value) if re.fullmatch(r"[0-9]{1,18}", remaining_value) else None
    retry_after = headers.get("retry-after", "")
    retry_seconds = re.fullmatch(r"[0-9]{1,18}", retry_after)
    retry_date = _HTTP_DATE.fullmatch(retry_after)
    reset = ""
    with suppress(OverflowError, OSError, ValueError):
        if epoch := headers.get("x-ratelimit-reset"):
            reset = datetime.fromtimestamp(int(epoch), UTC).isoformat()
    evidence = {
        "X-RateLimit-Resource": resource if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", resource) else None,
        "X-RateLimit-Remaining": remaining, "X-RateLimit-Reset": reset or None,
        "Retry-After": retry_after if retry_seconds or retry_date else None,
    }
    guidance = RATE_LIMIT_GUIDANCE
    if remaining == 0:
        guidance = f"\nRate limited: hourly quota exhausted: retry after {reset or 'the quota resets'}."
    elif remaining is not None or retry_seconds or retry_date:
        delay = f"{retry_after} s" if retry_seconds else retry_after if retry_date else "~60 s"
        guidance = f"\nRate limited: short-window limit: retry after {delay}."
    return "".join(f"\n{name}: {value}" for name, value in evidence.items() if value is not None) + guidance


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
        with suppress(OverflowError, OSError, ValueError):
            stamp = datetime.fromtimestamp(int(reset[1]), UTC)
            if stamp.timestamp() > time.time():
                return f"\nGraphQL quota resets at {stamp.isoformat()} (response header)."
    with suppress(ToolMissing, ProcTimeout, ReadDeadline, KeyError, TypeError, ValueError, OverflowError, OSError):
        query = "query { rateLimit { remaining resetAt } }"
        result = run(["gh", "api", "graphql", "-f", f"query={query}"], cwd=cwd,
                     env_allowlist=GH_ENV, extra_env={"GH_TOKEN": token} if token else None,
                     timeout=read_timeout(10))
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
    return ""


def _gh_json(*args: str, token: str | None = None, **kwargs) -> object:
    out = _gh(*args, token=token, **kwargs)
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


def _iter_paged(endpoint: str, key: str | None = None, **kwargs) -> Iterator[dict]:
    """Yield validated rows of a list (or of `key` in an envelope) until the last of at most 100 pages."""
    seen: set[bytes] = set()
    for page in range(1, 101):
        chunk = _gh_json("api", endpoint, "--method", "GET", "-F", f"per_page={PAGE}",
                         "-F", f"page={page}", conditional=True, **kwargs)
        if key is not None:
            chunk = chunk.get(key) if isinstance(chunk, dict) else None
        if (not isinstance(chunk, list) or len(chunk) > PAGE
                or not all(isinstance(row, dict) for row in chunk)):
            raise GhError(("gh", "api", endpoint), "invalid paginated response")
        if len(chunk) == PAGE:
            record_full_page(endpoint, chunk, seen)
        yield from chunk
        if len(chunk) < PAGE:
            return
    raise GhError(("gh", "api", endpoint), "pagination exceeds 100 pages")


def paged(endpoint: str, key: str | None = None, **kwargs) -> list[dict]:
    return list(_iter_paged(endpoint, key, **kwargs))


def api_get(endpoint: str, **kwargs) -> object:
    """`gh api <endpoint>` parsed as JSON (None on an empty body)."""
    return _gh_json("api", endpoint, conditional=True, **kwargs)


def job_log(repo: str, job_id: str) -> Completed:
    return run(["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs",
                "--hostname", "github.com", "--allow-escape-sequences"], cwd=Path.cwd(), env_allowlist=GH_ENV,
               timeout=read_timeout(5), encoding="utf-8", merge_output=True, tail_chars=256 * 1024)


def response_date(value: str) -> datetime | None:
    if _HTTP_DATE.fullmatch(value):
        with suppress(OverflowError, ValueError):
            return parsedate_to_datetime(value)
    return None


def auth_token() -> str:
    """Token of the authenticated `gh` host, for Git transports that cannot call gh."""
    return _gh("auth", "token", "--hostname", "github.com").strip()


_logins: dict[str, str] = {}


def login(token: str | None = None) -> str:
    """Login of the identity used for GitHub reads and review publishing.

    Read once per explicit, app or environment credential, filed under the credential the answering attempt
    used; an ambient gh account is read every time."""
    selected = _select_token(token, _read_scope.get(), True)
    if name := _logins.get(_cache_credential(selected)):
        return name
    used: list[str] = []
    data = _gh_json("api", "graphql", "-f", "query=query { viewer { login } }", token=token, used=used)
    content = data.get("data") if isinstance(data, dict) else None
    if isinstance(content, dict) and isinstance(viewer := content.get("viewer"), dict) and (name := viewer.get("login")):
        if used and (credential := _cache_credential(used[0])):  # of the attempt that answered
            _logins[credential] = str(name)
        return str(name)
    raise GhError(("gh", "api", "graphql"), "no login in viewer response")


def _api(endpoint: str, payload: dict, method: str = "POST",
         token: str | None = None, *, timeout: float = 120) -> object:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".loopzero", encoding="utf-8", delete_on_close=False
    ) as handle:
        handle.write(json.dumps(payload))
        handle.close()
        return _gh_json("api", endpoint, "--method", method, "--input", handle.name, token=token, timeout=timeout)


def merge_hold(number: int, labels: tuple[str, ...]) -> str | None:
    return f"PR #{number} has a merge-authority hold; ask the decisions owner to release it" if "hold" in labels else None


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
                or not is_sha(head_sha)):
            raise ValueError("invalid PR state, mergeability, or head")
        return PR(
            number=number, url=url, head_sha=head_sha, base_ref=base_ref, is_draft=draft,
            state="MERGED" if merged else state.upper(),
            mergeable="UNKNOWN" if mergeable is None else
                      ("MERGEABLE" if mergeable else "CONFLICTING"),
            merge_state=merge_state.upper() if merge_state else "UNKNOWN",
            author=str((data.get("user") or {}).get("login") or ""),
            body=str(data.get("body") or ""), labels=_pr_labels(data),
            node_id=data["node_id"] if isinstance(data.get("node_id"), str) else "",
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        number = data.get("number", "?") if isinstance(data, dict) else "?"
        raise GhError(("gh", "api"), f"malformed PR detail for #{number} ({exc!r})") from exc


def _pr_detail(repo: str, number: int) -> tuple[PR, dict]:
    endpoint = f"repos/{repo}/pulls/{number}"
    data = api_get(endpoint)
    pr = pr_from_rest(data)
    if pr.number != number:
        raise GhError(("gh", "api", endpoint), f"PR detail number {pr.number} differs from requested {number}")
    return pr, data


def _matches_branch(source: dict, repo: str, branch: str) -> bool:
    source_repo = source.get("repo")
    name = source_repo.get("full_name") if isinstance(source_repo, dict) else None
    return source.get("ref") == branch and isinstance(name, str) and name.casefold() == repo.casefold()


def _branch_sha(endpoint: str, field: str, invalid: str, *, full_sha: bool = False) -> str | None:
    try:
        ref = api_get(endpoint)
    except GhError as exc:
        if re.search(r"\bHTTP 404\b", exc.tail):
            return None
        raise
    ref_data = ref.get(field) if isinstance(ref, dict) else None
    sha = ref_data.get("sha") if isinstance(ref_data, dict) else None
    if not isinstance(sha, str) or not sha or (full_sha and not is_sha(sha)):
        raise GhError(("gh", "api", endpoint), invalid)
    return sha


def _matches_commit_pr(row: dict, repo: str, branch: str, endpoint: str) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("head"), dict):
        raise GhError(("gh", "api", endpoint), "invalid commit PR head")
    if not _matches_branch(row["head"], repo, branch):
        return False
    if (row.get("state") not in ("open", "closed")
            or type(row.get("number")) is not int or row["number"] < 1
            or not isinstance(row.get("created_at"), str)):
        raise GhError(("gh", "api", endpoint), "invalid commit PR")
    return True


def _commit_branch_prs(repo: str, branch: str, head_sha: str | None = None) -> tuple[str, list[dict]]:
    sha = head_sha
    if sha is None:
        sha = _branch_sha(f"repos/{repo}/branches/{quote(branch, safe='')}", "commit",
                          "invalid remote branch SHA", full_sha=True)
    if sha is None:
        return "", []

    endpoint = f"repos/{repo}/commits/{sha}/pulls"
    rows = _iter_paged(endpoint)
    try:
        first = list(islice(rows, 1))  # the first request
    except GhError as exc:
        # A local head can be unpushed. Let the branch fallback find its actual PR.
        missing_commit = re.search(r"\bHTTP 404\b", exc.tail) or (
            f"No commit found for SHA: {sha}" in exc.tail and re.search(r"\bHTTP 422\b", exc.tail)
        )
        if head_sha is not None and missing_commit:
            return endpoint, []
        raise
    return endpoint, [row for row in chain(first, rows) if _matches_commit_pr(row, repo, branch, endpoint)]


def _confirm_branch_pr(repo: str, branch: str, endpoint: str, number: int, state: str) -> PR:
    pr, detail = _pr_detail(repo, number)
    matches_state = pr.state == state or (state == "CLOSED" and pr.state == "MERGED")
    if not matches_state or not _matches_branch(detail["head"], repo, branch):
        lookup = "completion" if state == "MERGED" else "branch"
        raise GhError(("gh", "api", endpoint), f"PR changed during {lookup} lookup")
    return pr


def pr_for_branch(repo: str, branch: str, *, open_only: bool = False, head_sha: str | None = None) -> PR | None:
    """Return the open PR, falling back to the most recent closed PR unless open_only."""
    endpoint, matches = _commit_branch_prs(repo, branch, head_sha)
    newest = max((row for row in matches if row["state"] == "open"),
                 key=lambda row: (row["created_at"], row["number"]), default=None)
    if newest is not None:
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
                return _confirm_branch_pr(repo, branch, endpoint, number, "MERGED")
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
    if (pr.state != "OPEN" or not pr.is_draft or pr.base_ref != base
            or not _matches_branch(detail["head"], repo, branch)):
        raise GhError(("gh", "api", f"repos/{repo}/pulls"), "created PR differs from request")
    return pr


def update_body(repo: str, number: int, body: str) -> None:
    _api(f"repos/{repo}/pulls/{number}", {"body": body}, method="PATCH", token=app_token())


def finding_id(head_sha: str, path: str | None, line: int | None, title: str) -> str:
    raw = f"{head_sha}{path or ''}{line if line is not None else ''}{title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def finding_marker(severity: str, head_sha: str, finding: Finding) -> str:
    stable_id = finding_id(head_sha, finding.path, finding.line, finding.title)
    return f"<!-- loopzero:finding v=1 severity={severity} head={head_sha} id={stable_id} -->"


def _comment_body(f: Finding, head_sha: str, prefix: str = "") -> str:
    text = runners._redact_keys(None, f"{prefix}**{f.severity}: {f.title}**\n\n{f.body}").rstrip()
    if f.severity in BLOCKING:
        return f"{finding_marker(f.severity, head_sha, f)}\n{text}"
    return text


ReviewSettingsLayout = Literal["current", "requested-reported"]


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
            raise runners.RunnerBadOutput("codex: oversized leading machine block. To publish a review, split the PR and review the smaller changes.")
    severities = [f.severity for f in result.findings]
    summary = ", ".join(f"{severities.count(s)} {s}" for s in ("critical", "important", "suggestion"))
    duration = "unknown" if result.duration_s is None else f"{result.duration_s:.1f}s"
    lines = [prefix.rstrip("\n")] if prefix else []
    lines.append(
        f"loopzero {result.kind} review by {result.family} on `{result.head}`, "
        f"{duration}: **{result.verdict}** ({summary})"
    )
    if result.fallback:
        lines.append(f"\nSame-family fallback: {result.fallback}. Cross-family review is preferred.")
    if result.chunk_count > 1:
        lines.append(f"\nReviewed in {result.chunk_count} chunks with the same model family.")
    lines.extend(_review_settings_lines(result, include_configured=settings_layout == "current"))
    blocker_prefix, findings = _unplaced_findings(unplaced)
    return blocker_prefix + "\n".join(lines + findings) + _review_transcript(result, legacy_display)


def _unplaced_findings(unplaced: list[Finding]) -> tuple[str, list[str]]:
    if not unplaced:
        return "", []
    blocked = any(f.severity in BLOCKING for f in unplaced)
    lines = ["\nFindings without a commentable line:\n" if blocked else "\nFindings without a file location:\n"]
    for finding in unplaced:
        where = f" ({finding.path}:{finding.line})" if finding.path and finding.severity in BLOCKING else ""
        line = f"- **{finding.severity}**: {finding.title}{where} — {finding.body}".rstrip(" —")
        lines.append(runners._redact_keys(None, line))
    return (BODY_BLOCKERS if blocked else ""), lines


def _review_settings_lines(result: ReviewResult, *, include_configured: bool) -> list[str]:
    claims = "launch configuration and CLI claims" if include_configured else "CLI claims"
    lines = [f"\nSettings per native call ({claims}, not backend attestation):"]
    for index, call in enumerate(review_settings(result), 1):
        request, reported = call["requested"], call["reported"]
        if result.family == "codex":
            request, reported = ({key: value if runners._identifier(value, 128 if key == "model" else 32) else None
                                  for key, value in settings.items()} for settings in (request, reported))
        fields = [("requested", request), ("CLI-reported", reported)]
        if include_configured:
            fields.append(("configured", call["configured"]))
        settings = "; ".join(f"{label} model {value['model'] or 'unknown'} "
                             f"(effort {value['effort'] or 'unknown'})" for label, value in fields)
        lines.append(f"- Call {index}: {settings}")
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
        tail = encoded[-30_000:].decode("utf-8", errors="ignore")
        raw = ("[Transcript truncated; showing the last at most 30,000 UTF-8 bytes. "
               "Full output remains in the saved local review artifact.]\n" + tail)
    heading = "Raw reviewer output" if legacy_display else "Review evidence notice"
    return f"\n\n<details><summary>{heading}</summary>\n\n```\n{raw}\n```\n</details>"


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


def _diff_map(repo: str, number: int, head_sha: str,
              findings: tuple[Finding, ...]) -> tuple[dict[str, set[int]], dict | None]:
    blocking = tuple(f for f in findings if f.severity in BLOCKING)
    remaining = {f.path for f in findings if f.path and (f.line or f.severity in BLOCKING)}
    commentable: dict[str, set[int]] = {}
    anchor: dict | None = None
    endpoint = f"repos/{repo}/pulls/{number}/files"
    rows = _iter_paged(endpoint) if remaining or blocking else ()
    _count = 0
    for _count, entry in enumerate(rows, 1):
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
    if missing is not None and _count >= PR_FILE_LIMIT:
        pr, detail = _pr_detail(repo, number)
        request = ("gh", "api", endpoint)
        if pr.head_sha != head_sha:
            raise GhError(request, f"PR #{number} head moved from {head_sha[:12]} to {pr.head_sha[:12]}; rerun review")
        if detail.get("changed_files") != len(commentable):
            raise GhError(request, f"cannot place finding on PR #{number} at {missing!r}: "
                          f"GitHub's {PR_FILE_LIMIT:,}-file limit prevents verifying the complete file list")
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
        elif f.severity not in BLOCKING or anchor is None:
            loose.append(f)
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
    base: str | None = None,
) -> None:
    """Post `result` as a PR review on `head_sha`, one inline comment per finding.

    `body_prefix`, when given, becomes the first line of the review body. An App
    token, or a token whose user authored the PR (`pr` is fetched if not given),
    posts a COMMENT; only the operator identity approves or requests changes.

    Findings whose location is missing or outside the diff are anchored on the first
    changed file with a hunk. Without an anchor, blockers go in the review body.
    A supplied publisher is the login already checked for the supplied token.
    A supplied base makes every POST first reread the live PR, which must be open at `head_sha` on that base.
    """
    author = (pr or pr_view(repo, number)).author
    token = app_token() if token is None else token  # "" means the operator identity
    if token or (author and author == (publisher or login(token=token))):
        event = "COMMENT"
    else:
        event = "APPROVE" if result.verdict == "approve" else "REQUEST_CHANGES"
    payload = {**review_payload(repo, number, head_sha, result, body_prefix), "event": event}
    endpoint = f"repos/{repo}/pulls/{number}/reviews"
    def post(body: dict) -> None:
        if base and (live := pr_view(repo, number)) and (live.head_sha, live.state, live.base_ref) != (head_sha, "OPEN", base):
            raise GhError(("gh", "api", endpoint), f"PR is {live.state.lower()} at {live.head_sha[:12]} on {live.base_ref}")
        _api(endpoint, body, token=token)

    try:
        post(payload)
    except GhError as exc:
        if event == "COMMENT" or not _SELF_REVIEW.search(exc.tail):
            raise
        post({**payload, "event": "COMMENT"})


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
                  publishers: tuple[str, ...]) -> tuple[str, Finding] | None:
    match = _thread_marker(node, publishers)
    if not match or match.group("severity") not in BLOCKING:
        return None
    body = node["comments"]["nodes"][0]["body"]
    marker_head = match.group("head")
    if current_head is not None and marker_head != current_head and node.get("isOutdated"):
        return None
    rest = body[match.end():].strip()
    return match.group("id"), Finding(severity=match.group("severity"), path=node.get("path"), line=node.get("line"),
                                      title=_title_of(rest), body=rest)


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
        (parsed[0], node["id"], parsed[1])
        for node in _review_threads(repo, number)
        if (parsed := _parse_thread(node, None, publishers))
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
                        "-f", f"thread={node['id']}", "-f", f"after={cursor}", token="")
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
    with pr_lock(repo, number):
        threads = _review_threads(repo, number, include_resolved=True)
        for node in sorted(threads, key=lambda node: bool(node.get("isResolved"))):
            parsed = _parse_thread(node, None, publishers)
            if parsed is None or parsed[0] != finding_id:
                continue
            _, finding = parsed
            confirmed = _has_thread_reply(node, reply)
            if node.get("isResolved"):
                if confirmed:
                    return finding
                continue
            if not confirmed:
                # Two calls on purpose: fields of one mutation are not transactional, and a
                # thread must never be resolved unless its reply was confirmed.
                posted = _gh_json("api", "graphql", "-f", f"query={_REPLY_MUTATION}",
                                  "-f", f"thread={node['id']}", "-f", f"body={reply}")
                try:
                    posted["data"]["addPullRequestReviewThreadReply"]["comment"]["id"]
                except (KeyError, TypeError) as exc:
                    raise GhError(("resolve", finding_id), "reply was not posted; not resolving") from exc
            try:
                response = _gh("api", "graphql", "-f", f"query={_RESOLVE_MUTATION}",
                               "-f", f"thread={node['id']}", retry_idempotent=True)
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
        raise GhError(("resolve", finding_id), f"no open blocking finding {finding_id} on #{number}. Run `loopzero resolve` to list open IDs.")


def eligibility_dispatch_args(repo: str, number: int, base: str) -> tuple[str, ...]:
    return ("api", "-X", "POST", f"repos/{repo}/actions/workflows/eligibility.yml/dispatches",
            "-f", f"ref={base}", "-f", f"inputs[pr]={number}")


def _eligibility_dispatched(repo: str, base: str, since: str) -> bool:
    query = f"event=workflow_dispatch&branch={quote(base, safe='')}&created={quote('>=' + since, safe='')}&per_page=100"
    data = api_get(f"repos/{repo}/actions/workflows/eligibility.yml/runs?{query}")
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    complete = isinstance(runs, list) and data.get("total_count") == len(runs)
    if not complete or not all(isinstance(run, dict) for run in runs):
        raise GhError(("refresh Loop-zero Eligibility",), "could not identify all recent refresh runs")
    return bool(runs)  # Consumer workflows set no run title; the dispatch window identifies the run.


def refresh_eligibility(repo: str, pr: PR, branch: str, base: str, publishers: tuple[str, ...], *,
                        required: bool = True) -> bool:
    args = eligibility_dispatch_args(repo, pr.number, base)
    since = datetime.fromtimestamp(time.time(), UTC).isoformat(timespec="seconds")
    try:
        try:
            _gh(*args)
        except (GhUnavailable, ProcTimeout) as exc:
            if isinstance(exc, GhUnavailable) and not _transient_error(exc.tail):
                raise
            _retry_sleep(RETRY_DELAY)
            current = pr_for_branch(repo, branch, open_only=True)
            if current is None or (current.number, current.head_sha, current.base_ref) != (
                pr.number, pr.head_sha, pr.base_ref,
            ):
                raise GhError(("refresh Loop-zero Eligibility",), "the PR or head changed after resolution") from exc
            if _eligibility_dispatched(repo, base, since):
                return True
            if open_blocking_findings(repo, pr.number, None, publishers):
                raise GhError(("refresh Loop-zero Eligibility",), "blocking findings appeared after resolution") from exc
            _gh(*args)
    except (GhError, ProcTimeout, OSError) as exc:
        if (isinstance(exc, GhError) and exc.command[:len(args) + 1] == ("gh", *args)
                and re.search(r"\bHTTP 404\b", exc.tail)):
            if not required:
                return False
            raise GhError(("refresh Loop-zero Eligibility",),
                          f"Finding on PR #{pr.number} is resolved. Check that eligibility.yml exists on "
                          f"{base} and your GitHub login can dispatch it.") from exc
        retry = shlex.join(("gh", *args))
        raise GhError(("refresh Loop-zero Eligibility",),
                      f"Finding on PR #{pr.number} is resolved. {exc}\n"
                      f"Verify the PR head, remaining findings and recent runs before retrying: {retry}") from exc
    return True


def open_blocking_findings(
    repo: str, number: int, current_head: str | None, publishers: tuple[str, ...],
) -> list[Finding]:
    """Unresolved findings with a trusted first-line marker, filtering outdated heads when supplied."""
    return [parsed[1] for node in _review_threads(repo, number)
            if (parsed := _parse_thread(node, current_head, publishers))]


def _pull_query(repo: str, number: int, query: str) -> tuple[str, ...]:
    owner, name = repo.split("/", 1)
    return ("api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
            "-f", f"name={name}", "-F", f"number={number}")


def _review_threads(repo: str, number: int, *, include_resolved: bool = False) -> list[dict]:
    args = _pull_query(repo, number, _THREADS_QUERY)
    seen: set[str] = set()
    found: list[dict] = []
    cursor: tuple[str, ...] = ()
    while True:
        data = _gh_json(*args, *cursor)
        try:
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            page = threads["pageInfo"]
            found.extend(node for node in threads["nodes"] if include_resolved or not node.get("isResolved"))
            if not page["hasNextPage"]:
                return found
            cursor = ("-f", f"after={_next_thread_cursor(page, seen)}")
        except (KeyError, TypeError) as exc:
            raise GhError(("gh", "api"), f"PR #{number} not found or not accessible ({exc!r})") from exc


_ACTIVE_WORKFLOWS = {"queued", "in_progress", "waiting", "pending", "requested"}
_PENDING_CHECKS = _ACTIVE_WORKFLOWS | {"expected"}
_NONFAILING_CHECKS = _PENDING_CHECKS | {"success"}
_FAILED_VERDICTS = {"failure", "error", "cancelled", "timed_out"}


ReadinessTimestamp = tuple[datetime, str]


def readiness_timestamp(value: object) -> ReadinessTimestamp | None:
    if isinstance(value, str) and (match := _READINESS_TIMESTAMP.fullmatch(value)):
        with suppress(OverflowError, ValueError):
            return datetime.fromisoformat(value).astimezone(UTC), (match[1] or "")[6:].rstrip("0")
    return None


def _check_signals(
    repo: str,
    head_sha: str,
    review_at: ReadinessTimestamp | None = None,
    review_grace: bool = False,
    required_ci: tuple[str, ...] = (),
    stale_verdict_grace: bool = False,
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...], bool]:
    signals: dict[str, list[tuple[str, ReadinessTimestamp | None]]] = {}
    latest_runs: dict[tuple[str, int | None], dict] = {}
    runs = paged(f"repos/{repo}/commits/{head_sha}/check-runs", "check_runs")
    for cr in runs:
        key = cr["name"], (cr.get("app") or {}).get("id")
        latest_runs[key] = max(latest_runs.get(key, cr), cr, key=_check_run_order)
    for (name, _), cr in latest_runs.items():
        state = cr.get("conclusion") or cr.get("status") or "unknown"
        timestamp = cr.get("started_at" if state in _PENDING_CHECKS else "completed_at")
        signals.setdefault(name, []).append((state, readiness_timestamp(timestamp)))
    latest_status: dict[str, dict] = {}
    for status in paged(f"repos/{repo}/commits/{head_sha}/statuses"):
        latest_status.setdefault(status["context"], status)
    for name, status in latest_status.items():
        signals.setdefault(name, []).append((status.get("state") or "unknown", readiness_timestamp(status.get("created_at"))))
    states = {name: _aggregate_signal([state for state, _ in values]) for name, values in signals.items()}
    stale = tuple(name for name, values in signals.items() if review_at
                  and states[name] not in _NONFAILING_CHECKS
                  and all(at and at < review_at for state, at in values if state not in _NONFAILING_CHECKS)
                  and (review_grace or any(at and at >= review_at for state, at in values if state in _PENDING_CHECKS)))
    stale_verdicts = tuple(name for name, values in signals.items() if review_at and stale_verdict_grace
                           and name not in stale and states[name] in _FAILED_VERDICTS
                           and all(at and at < review_at for state, at in values if state != "success"))
    stale += stale_verdicts
    superseded: list[str] = []
    workflows: list[dict] | None = None
    for (name, _), cr in latest_runs.items():
        if (name not in required_ci or name in stale
                or cr.get("conclusion") not in {"failure", "cancelled", "timed_out"}
                or cr.get("status") != "completed" or not (cr.get("check_suite") or {}).get("id")
                or sum(state not in _NONFAILING_CHECKS for state, _ in signals[name]) != 1):
            continue
        if workflows is None:
            workflows = paged(f"repos/{repo}/actions/runs?head_sha={head_sha}", "workflow_runs")
        if _has_active_replacement(repo, cr, workflows, head_sha):
            superseded.append(name)
    capped = _check_suites_at_limit(runs)
    # A status-only success may hide a failed run outside the returned suites.
    if capped:
        run_names = {name for name, _ in latest_runs}
        states = {name: state for name, state in states.items() if state != "success" or name in run_names}
    return states, (*stale, *superseded), stale_verdicts, capped


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


def has_check_activity(repo: str, head_sha: str, readiness: Readiness) -> bool:
    if readiness.has_pending_checks:
        return True
    runs = _iter_paged(f"repos/{repo}/actions/runs?head_sha={head_sha}", "workflow_runs")
    return any(run.get("head_sha") == head_sha and run.get("status") in _ACTIVE_WORKFLOWS for run in runs)


def _has_active_replacement(repo: str, check: dict, workflows: list[dict], head_sha: str) -> bool:
    """A producer can be superseded only by its own workflow on the same head."""
    suite_id = check["check_suite"]["id"]
    def find_producer(runs: list[dict]) -> dict | None:
        return next((run for run in runs if run.get("check_suite_id") == suite_id
                     and run.get("head_sha") == head_sha), None)
    producer = find_producer(workflows)
    if producer is None and len(workflows) >= 1_000 and type(suite_id) is int:
        producer = find_producer(paged(f"repos/{repo}/actions/runs?check_suite_id={suite_id}", "workflow_runs"))
    if producer is None or not producer.get("workflow_id"):
        return False
    created = readiness_timestamp(producer.get("created_at"))
    for workflow in workflows:
        if (workflow.get("head_sha") != head_sha
                or workflow.get("workflow_id") != producer["workflow_id"]
                or workflow.get("status") not in _ACTIVE_WORKFLOWS):
            continue
        if workflow.get("id") == producer.get("id"):
            if (workflow.get("run_attempt", 1) > 1
                    and (completed := readiness_timestamp(check.get("completed_at"))) is not None
                    and (started := readiness_timestamp(workflow.get("run_started_at"))) is not None
                    and started > completed):
                return True
        elif (created is not None and isinstance(producer.get("id"), int)
              and (replacement := readiness_timestamp(workflow.get("created_at"))) is not None
              and isinstance(workflow.get("id"), int)
              and (replacement, workflow["id"]) > (created, producer["id"])):
            return True
    return False


def _check_run_order(run: dict) -> int:
    """GitHub check-run ids are unique and increase, so the highest id is the latest run."""
    run_id = run.get("id")
    return run_id if isinstance(run_id, int) else -1


def _aggregate_signal(states: list[str]) -> str:
    if all(state == "success" for state in states):
        return "success"
    return next((state for state in states if state not in _NONFAILING_CHECKS), "pending")


def behind_reason(pr: PR) -> str:
    return (
        f"PR #{pr.number} is behind {pr.base_ref}. Run "
        f"`git fetch origin {shlex.quote(pr.base_ref)}`, "
        f"`git merge {shlex.quote(f'origin/{pr.base_ref}')}`, `loopzero check` and `loopzero pr` before retrying readiness."
    )


def mergeability_blocker(pr: PR) -> str | None:
    if pr.mergeable == "CONFLICTING":
        return f"PR #{pr.number} has merge conflicts with {pr.base_ref}. Run `git fetch origin {shlex.quote(pr.base_ref)}` and `git merge {shlex.quote(f'origin/{pr.base_ref}')}`. Resolve conflicts, run `loopzero check` and `loopzero pr`, then follow the review guidance from `loopzero ready`."
    if pr.state == "OPEN" and pr.mergeable == "UNKNOWN":
        return MERGEABILITY_PENDING
    return None


def readiness(
    repo: str,
    pr: PR,
    base_branch: str,
    required_ci: tuple[str, ...],
    reviewed_head: str | None,
    *, allow_behind: bool = False, ancestor_review: bool = False,
    review_at: ReadinessTimestamp | None = None,
    review_grace: bool = False,
    workflow_wait: bool = False,
    stale_verdict_grace: bool = False, review_blocker: str | None = None,
    publishers: tuple[str, ...],
) -> Readiness:
    """Decide whether `pr` may be marked ready / merged. Never raises on policy failures."""
    reasons: list[str] = []
    if pr.state != "OPEN":
        reasons.append(f"PR #{pr.number} is {pr.state}, not open")
    if pr.base_ref != base_branch:
        reasons.append(f"PR targets {pr.base_ref}, configured base is {base_branch}. Retarget the PR on GitHub and retry.")
    if blocker := mergeability_blocker(pr):
        reasons.append(blocker)
    if pr.merge_state == "BEHIND" and not allow_behind:
        reasons.append(behind_reason(pr))
    if review_blocker or not reviewed_head:
        reasons.append(review_blocker or MISSING_REVIEW_REASON)
    elif pr.head_sha != reviewed_head:
        reasons.append(f"head {pr.head_sha[:12]} differs from reviewed {reviewed_head[:12]}. Run `loopzero review` for the current head.")
    for f in open_blocking_findings(repo, pr.number, None if ancestor_review or not reviewed_head else pr.head_sha,
                                    publishers):
        where = f"{f.path}:{f.line}" if f.path else "(no location)"
        reasons.append(f"open {f.severity} finding at {where}: {f.title}. Run `loopzero resolve` to list finding IDs and address the review.")
    checks, stale, stale_verdicts, capped = (_check_signals(
        repo, pr.head_sha, review_at, review_grace, required_ci if workflow_wait else (),
        workflow_wait and stale_verdict_grace,
    ) if required_ci else ({}, (), (), False))
    failures = {
        name: f"required check '{name}' is {checks[name]}" for name in required_ci
        if name in checks and checks[name] != "success"
    }
    check_reasons = {}
    for name in required_ci:
        if name not in checks or name in failures:
            reasons.append(reason := failures.get(name) or _missing_check_reason(name, pr.head_sha, capped))
            check_reasons[reason] = (name, checks.get(name) or ("capped" if capped else "missing"))
    return Readiness(
        ready=not reasons, reasons=tuple(reasons), checks=check_reasons,
        waitable_failures=tuple(failures[name] for name in required_ci if name in stale
                               or (workflow_wait and review_grace and checks.get(name) in _FAILED_VERDICTS)),
        stale_verdicts=tuple(name for name in required_ci if name in stale_verdicts),
        has_pending_checks="pending" in checks.values(),
    )


def mark_ready(repo: str, number: int, node_id: str = "") -> None:
    if node_id:
        _gh("api", "graphql", "-f", "query=mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id}}}",
            "-f", f"id={node_id}", timeout=read_timeout(120))
    else:
        _gh("pr", "ready", str(number), "--repo", repo, timeout=read_timeout(120))


_QUEUE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){headRefOid isInMergeQueue autoMergeRequest{enabledAt}}}}"
)


class ClosedUnmerged(GhError):
    """A PR read as closed but not merged; right after a queue merge this can be transient."""


def merged_sha(
    repo: str, number: int, *, expected_head: str | None = None, check_hold: bool = False,
    expected_base: str | None = None,
) -> str | None:
    argv = ("gh", "api", f"repos/{repo}/pulls/{number}")
    view = api_get(argv[2])
    if not isinstance(view, dict) or type(merged := view.get("merged")) is not bool:
        raise GhError(argv, "invalid PR merge evidence")
    if expected_head and (not isinstance(view.get("head"), dict)
                          or view["head"].get("sha") != expected_head):
        raise GhError(argv, "PR head changed before merge verification; sync the branch and run loopzero ready")
    if expected_base is not None:
        base = view.get("base")
        if not isinstance(base, dict) or not isinstance(ref := base.get("ref"), str):
            raise GhError(argv, "invalid PR base evidence; inspect the PR before retrying")
        if ref != expected_base:
            raise GhError(argv, f"PR retargeted to {ref}; restore {expected_base} before rerunning loopzero merge --wait")
    state = view.get("state")
    if merged and state == "closed" and is_sha(sha := view.get("merge_commit_sha")):
        return sha
    if not merged and state == "open":
        if check_hold and (hold := merge_hold(number, _pr_labels(view))):
            raise GhError(argv, hold)
        return None  # An open PR's merge_commit_sha is only a test merge.
    error = ClosedUnmerged if not merged and state == "closed" else GhError
    raise error(argv, "PR closed without a verified merge or invalid merge evidence; inspect the PR before retrying")


def merge_pending(repo: str, number: int, head_sha: str) -> bool:
    args = _pull_query(repo, number, _QUEUE_QUERY)
    try:
        pr = _gh_json(*args)["data"]["repository"]["pullRequest"]
        queued = pr["isInMergeQueue"]
        auto = pr.get("autoMergeRequest")
        if type(queued) is not bool or (auto is not None and not isinstance(auto, dict)):
            raise ValueError("invalid queue fields")
    except (GhError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GhUnavailable):
            raise
        raise MergeFailed(("gh", "api", "graphql"),
                          f"GitHub queue lookup for PR #{number}: queue status could not be verified. "
                          f"Check the PR's queue or merge state before retrying.\n{str(exc)[-300:]}") from exc
    if pr.get("headRefOid") != head_sha:
        raise MergeFailed(("gh", *args), "PR head changed or unavailable during queue verification")
    return queued or bool(auto)


def _merge_evidence(repo: str, number: int, head_sha: str, base: str) -> tuple[str | None, bool, GhError | ProcTimeout | None]:
    if sha := merged_sha(repo, number, expected_head=head_sha, expected_base=base):
        return sha, False, None
    queue_error = None
    try:
        if merge_pending(repo, number, head_sha):
            return None, True, None
    except (MergeFailed, GhUnavailable, ProcTimeout) as exc:
        if isinstance(exc, ReadDeadline):
            raise
        queue_error = exc
    # The queue may have landed the PR between the two reads.
    return merged_sha(repo, number, expected_head=head_sha, expected_base=base), False, queue_error


def merge(repo: str, number: int, strategy: str, head_sha: str, base: str) -> str | None:
    if strategy not in ("squash", "merge", "rebase", "queue"):
        raise MergeFailed(("gh", "pr", "merge"), f"unknown merge strategy: {strategy}")
    # A merge queue owns the method; gh rejects an explicit --squash/--merge/--rebase.
    method = () if strategy == "queue" else (f"--{strategy}",)
    argv = ("pr", "merge", str(number), "--repo", repo, *method, "--match-head-commit", head_sha)
    if strategy == "queue" and merge_pending(repo, number, head_sha):
        return None
    if sha := merged_sha(repo, number, expected_head=head_sha, expected_base=base, check_hold=True):
        return sha
    read_timeout(0)
    write_error = None
    try:
        _gh(*argv, timeout=300)
    except (GhError, ProcTimeout, OSError) as exc:
        write_error = exc
    scope = _read_scope.set(_read_scope.get() if strategy == "queue" else None)
    try:
        sha, queued, queue_error = _merge_evidence(repo, number, head_sha, base)
        if strategy == "queue":
            read_timeout(0)  # Queue evidence read after the wait budget is a timeout.
    except (GhError, ProcTimeout) as exc:
        if strategy == "queue" or isinstance(exc, (MergeFailed, ClosedUnmerged)):
            raise
        raise MergeFailed(("gh", *argv), f"PR #{number} merge could not be verified: {exc}") from exc
    finally:
        _read_scope.reset(scope)
    if sha or queued:
        return sha
    if queue_error:
        raise queue_error
    detail = f": {write_error}" if write_error else ""
    raise MergeFailed(("gh", *argv), f"PR #{number} neither merged nor queued{detail}")


def delete_remote_branch(repo: str, branch: str, expected_head: str) -> bool:
    """Return False if the remote ref moved, or True if deleted or absent."""
    endpoint = f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"
    read_head = partial(_branch_sha, endpoint, "object", "missing remote ref SHA")
    current = read_head()
    if current != expected_head:
        return current is None
    ref = os.fsdecode(f"refs/heads/{branch}".encode("utf-8", "surrogateescape"))
    argv = ("git", "-c", "credential.helper=!gh auth git-credential",
            "push", f"--force-with-lease={ref}:{expected_head}",
            f"https://github.com/{repo}.git", f":{ref}")
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="loopzero-cleanup-") as scratch:
        init = ("git", "init", "--bare", "--template=")
        done = run(init, cwd=scratch, env_allowlist=GH_BASE_ENV, extra_env=env, timeout=120)
        if done.exit_code != 0:
            raise GhError(init, done.stderr or done.stdout)
        done = run(argv, cwd=scratch, env_allowlist=GH_ENV, extra_env=env, timeout=120)
    if done.exit_code == 0:
        return True
    if (current := read_head()) == expected_head:
        raise GhError(argv, done.stderr or done.stdout)
    return current is None


def commit_status(repo: str, head: str, context: str, state: str, description: str, *,
                  timeout: float = 120) -> None:
    """Publish a SHA-scoped status using the caller's narrowly scoped credential."""
    endpoint = f"repos/{repo}/statuses/{head}"
    try:
        _api(endpoint, {"state": state, "context": context, "description": description}, timeout=timeout)
    except GhError as exc:
        validation_error = re.search(r"\bHTTP 422\b|^method=POST endpoint=\S+ status=422\b", exc.tail)
        if validation_error and _status_limit_reached(repo, head, context):
            raise StatusLimitReached(
                exc.command, f"{head} context {context!r} reached GitHub's 1,000-status limit. "
                "Push a fresh head and obtain review evidence for it before refreshing eligibility."
            ) from exc
        raise


def context_statuses(repo: str, head: str, context: str) -> Iterator[dict]:
    return (r for r in _iter_paged(f"repos/{repo}/commits/{head}/statuses") if str(r.get("context", "")).casefold() == context.casefold())


def _status_limit_reached(repo: str, head: str, context: str) -> bool:
    # A validation error alone does not prove exhaustion.
    with suppress(GhError, ProcTimeout, OSError):
        return sum(1 for _ in context_statuses(repo, head, context)) >= 1_000
    return False
