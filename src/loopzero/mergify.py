"""Small fail-closed adapter for Mergify queue membership and command requests."""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import signal
import stat
import ssl
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import candidate, github
from .types import LoopZeroError

API = "https://api.mergify.com/v1"
REQUEST = "loopzero:mergify-request"
CONFIRMED = "loopzero:mergify-confirmed"


class MergifyError(LoopZeroError):
    """Authentication, transport, or response failure from Mergify."""


@dataclass(frozen=True)
class Membership:
    candidate_number: int | None = None
    unmet_conditions: tuple[str, ...] = ()


def _credential(path: Path | None = None) -> str:
    supplied = os.environ.get("MERGIFY_API_KEY", "").strip()
    if supplied:
        return supplied
    path = path or Path.home() / ".config" / "mergify" / "api-key"
    try:
        parent = path.parent.lstat()
    except FileNotFoundError as exc:
        raise MergifyError(
            "Mergify credentials unavailable; set MERGIFY_API_KEY or create "
            "~/.config/mergify/api-key"
        ) from exc
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700):
        raise MergifyError(f"unsafe Mergify credential directory permissions: {path.parent}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise MergifyError(f"unable to open Mergify credential file: {path}") from exc
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(fd)
        raise MergifyError(f"unsafe Mergify credential file permissions: {path}")
    with os.fdopen(fd, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise MergifyError(f"Mergify credential file is empty: {path}")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise MergifyError("Mergify API refused an HTTP redirect")


_retry_sleep = time.sleep


class _RequestDeadline(Exception):
    """Bypass transport handlers that retry OSError."""


@contextmanager
def _open(request: urllib.request.Request, timeout: float = 30):
    def expire(_signum, _frame):
        raise _RequestDeadline()

    if signal.getitimer(signal.ITIMER_REAL) != (0, 0):
        raise TimeoutError("Mergify HTTP request cannot start with an active alarm")
    try:
        with ExitStack() as cleanup:
            cleanup.callback(signal.signal, signal.SIGALRM, signal.signal(signal.SIGALRM, expire))
            cleanup.callback(signal.setitimer, signal.ITIMER_REAL, 0)
            signal.setitimer(signal.ITIMER_REAL, timeout)
            try:
                response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
            except ValueError:
                raise MergifyError("Mergify API request has an invalid URL or header") from None
            yield cleanup.enter_context(response)
    except _RequestDeadline:
        raise TimeoutError("Mergify HTTP request timed out") from None


def _api_request(repo: str, suffix: str, method: str | None = None) -> urllib.request.Request:
    owner, name = (quote(part, safe="") for part in repo.split("/", 1))
    return urllib.request.Request(
        f"{API}/repos/{owner}/{name}{suffix}", method=method,
        headers={"Accept": "application/json", "User-Agent": "loopzero",
                 "Authorization": f"Bearer {_credential()}"},
    )


def _wait_to_retry(delay: float, deadline: float | None) -> bool:
    remaining = 5.0 if deadline is None else deadline - time.monotonic()
    if delay >= remaining:
        return False
    _retry_sleep(delay)
    return deadline is None or time.monotonic() < deadline


def _queue_read_failure(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "Mergify queue lookup returned no valid JSON", True
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return f"Mergify queue transport failed: {reason}", not isinstance(reason, ssl.SSLCertVerificationError)


def api_get(repo: str, suffix: str, deadline: float | None = None) -> object | None:
    """GET one documented endpoint below this repository's fixed Mergify API path."""
    if not suffix.startswith("/") or "://" in suffix or ".." in suffix:
        raise ValueError("Mergify API suffix must be a repository-relative absolute path")
    request = _api_request(repo, suffix)
    for retry in (True, False):
        delay = 2.0
        try:
            with _open(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            wait = github.retry_after(exc.headers.get("Retry-After", "")) if exc.headers else None
            delay = max(delay, wait or 0.0)
            detail = f"Mergify queue lookup failed with HTTP {exc.code}" + (f"; Retry-After: {wait:g}s" if wait is not None else "")
            transient = exc.code >= 500 or exc.code == 429 or (exc.code == 403 and wait is not None)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, http.client.IncompleteRead) as exc:
            detail, transient = _queue_read_failure(exc)
        if not retry or not transient:
            raise MergifyError(detail)
        if not _wait_to_retry(delay, deadline):
            raise MergifyError(f"{detail}; retry delay exceeds the wait budget")
    return None

def _unmet_conditions(node: object) -> tuple[str, ...]:
    if not isinstance(node, dict) or node.get("match") is not False:
        return ()
    children = node.get("subconditions")
    if isinstance(children, list) and children:
        return tuple(label for child in children for label in _unmet_conditions(child))
    label = node.get("label")
    return (label,) if isinstance(label, str) and label.isprintable() else ()


def membership(repo: str, number: int, queue: str, deadline: float | None = None) -> Membership | None:
    data = api_get(repo, f"/merge-queue/pull/{number}", deadline)
    if data is None:
        return None
    try:
        if type(data["number"]) is not int or data["number"] != number:
            raise ValueError("pull request number mismatch")
        actual = str(data["queue_rule_name"])
        if actual != queue:
            raise ValueError(f"expected queue {queue!r}, got {actual!r}")
        if (not isinstance(data["queued_at"], str) or not data["queued_at"]
                or type(data["position"]) is not int or data["position"] < 0):
            raise ValueError("invalid queue position or admission time")
        check = data.get("mergeability_check")
        if not isinstance(check, dict):
            return Membership()
        candidate = check.get("queue_pull_request_number")
        return Membership(
            candidate if type(candidate) is int and candidate > 0 else None,
            _unmet_conditions(check.get("conditions_evaluation")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MergifyError("invalid Mergify queue membership response") from exc


def queue_predecessors(repo: str, branch: str, queue: str, number: int) -> tuple[int, ...]:
    """Identify a single chain of one-PR batches ahead of a new request."""
    status = api_get(repo, f"/merge-queue/status?branch={quote(branch, safe='')}")
    if (not isinstance(status, dict) or status.get("mode") != "serial"
            or status.get("waiting_pull_requests") != [] or status.get("forming_batches") != []):
        return ()
    try:
        batches = candidate.flatten_batches(status)
        successors: dict[str | None, tuple[str, int]] = {}
        for batch_id, batch in batches.items():
            pulls, parents = batch["pull_requests"], batch["parent_ids"]
            if pulls == [] and batch.get("sub_batches"):
                continue  # Container batches carry no direct source.
            if (batch.get("queue_rule_name") != queue or not isinstance(pulls, list)
                    or len(pulls) != 1 or not isinstance(parents, list) or len(parents) > 1):
                return ()
            source = pulls[0]["number"]
            parent = next(iter(parents), None)
            if type(source) is not int or source <= 0 or source == number or parent in successors:
                return ()
            successors[parent] = batch_id, source
        return _ordered_sources(successors)
    except (candidate.CandidateError, KeyError, TypeError, RecursionError):
        return ()


def _ordered_sources(successors: dict[str | None, tuple[str, int]]) -> tuple[int, ...]:
    if len(successors) > 32:
        return ()
    sources = []
    cursor = None
    while cursor in successors:
        cursor, source = successors.pop(cursor)
        sources.append(source)
    return tuple(sources) if not successors and len(sources) == len(set(sources)) else ()


def _preserves_source_heads(repo: str, branch: str, queue: str) -> bool:
    """Read the vendor's canonical input, not a deprecated raw queue-rule default."""
    import yaml

    class _ConfigLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            keys = [self.construct_object(key, deep=True) for key, _ in node.value]
            if len(keys) != len(set(keys)):
                raise yaml.YAMLError("duplicate configuration key")
            return super().construct_mapping(node, deep=deep)

    repo_path = f"repos/{repo}"
    metadata = github.api_get(repo_path)
    if not isinstance(metadata, dict) or metadata.get("default_branch") != branch:
        return False
    path = f"{repo_path}/contents/.mergify.yml?ref={quote('refs/heads/' + branch, safe='')}"
    blob = github.api_get(path)
    try:
        if (blob["type"] != "file" or blob["encoding"] != "base64"
                or not isinstance(blob["sha"], str) or len(blob["sha"]) != 40
                or any(char not in "0123456789abcdef" for char in blob["sha"])
                or not isinstance(blob["content"], str) or len(blob["content"]) > 90000):
            return False
        document = yaml.load(base64.b64decode("".join(blob["content"].split()), validate=True),
                             Loader=_ConfigLoader)
        if not isinstance(document, dict) or "extends" in document or "scopes" in document:
            return False
        mode = document["merge_queue"]
        parallel = mode["max_parallel_checks"]
        rules = document["queue_rules"] if isinstance(document["queue_rules"], list) else []
        matched = [r for r in rules if isinstance(r, dict) and r.get("name") == queue]
        if (mode.get("mode") != "serial" or type(parallel) is not int or parallel < 1
                or len(matched) != 1 or (parallel == 1  # one lone check rewrites heads in place
                and (type(batch := matched[0].get("batch_size")) is not int or batch <= 1))):
            return False
    except (KeyError, TypeError, ValueError, yaml.YAMLError, RecursionError):
        return False
    # File identity, not main's commit: unrelated merges must not invalidate this proof.
    current = github.api_get(path)
    metadata = github.api_get(repo_path)
    return (isinstance(current, dict) and current.get("sha") == blob["sha"]
            and isinstance(metadata, dict) and metadata.get("default_branch") == branch)


def configured(repo: str, branch: str, queue: str, deadline: float | None = None) -> bool:
    """Require live queue identity and stable canonical speculative configuration."""
    status_data = api_get(repo, f"/merge-queue/status?branch={quote(branch, safe='')}", deadline)
    if status_data is None:
        return False
    rules_data = api_get(repo, "/queues/configuration", deadline)
    if not isinstance(status_data, dict) or not isinstance(rules_data, dict):
        return False
    if not isinstance(status_data.get("batches"), list):
        raise MergifyError("invalid Mergify branch queue status response")
    rules = rules_data.get("configuration")
    if not isinstance(rules, list):
        raise MergifyError("invalid Mergify queue configuration response")
    return status_data.get("mode") == "serial" and sum(
        isinstance(rule, dict) and rule.get("name") == queue
        and isinstance(rule.get("config"), dict) for rule in rules
    ) == 1 and _preserves_source_heads(repo, branch, queue)


def dequeue(repo: str, number: int) -> None:
    request = _api_request(repo, f"/merge-queue/pull/{number}/dequeue", "POST")
    try:
        with _open(request):
            return
    except urllib.error.HTTPError as exc:
        raise MergifyError(f"Mergify dequeue failed with HTTP {exc.code}") from exc
    except OSError as exc:
        raise MergifyError("Mergify dequeue transport failed") from exc


def _marker(kind: str, head: str, queue: str) -> str:
    return f"<!-- {kind} head={head} queue={queue} -->"


def _comments_by(comments: list[dict], login: str):
    for comment in comments:
        if isinstance(user := comment.get("user"), dict) and user.get("login") == login:
            yield comment


def _updated_at(comment: dict) -> str:
    return str(comment.get("updated_at") or "")


def _admission_report(comments: list[dict], requested_at: str) -> int | None:
    latest = max(_comments_by(comments, "mergify[bot]"), key=_updated_at, default={})
    if (_updated_at(latest) >= requested_at
            and "### ⏳ Waiting for queue conditions" in str(latest.get("body") or "").splitlines()):
        report_id = latest.get("id")
        return report_id if type(report_id) is int and report_id > 0 else None
    return None


def _queue_command(body: str, queue: str) -> bool:
    words = body.partition("\n")[0].split()
    return (len(words) in (2, 3) and words[0].lower() in ("@mergify", "@mergifyio")
            and words[1].lower() in ("queue", "requeue") and words[2:] in ([], [queue]))


@dataclass(frozen=True)
class Departure:
    url: str
    checks: tuple[str, ...]
    attribution: str


def _departure_payload(body: str, queue: str) -> dict:
    _, _, text = body.partition("-*- Mergify Payload -*-\n")
    try:
        payload = json.loads(text.partition("\n-*- Mergify Payload End -*-")[0])
    except json.JSONDecodeError:
        return {}
    if (isinstance(payload, dict) and payload.get("version") == 1
            and payload.get("state") == "dequeued" and payload.get("queue_rule_name") == queue):
        return payload
    return {}


def departure_report(repo: str, number: int, head: str, queue: str) -> Departure | None:
    """Use a bot report only when it identifies the latest command and current head."""
    try:
        comments = github._paged(f"repos/{repo}/issues/{number}/comments")
    except github.GhError:
        return None
    comments = [item for item in comments if type(item.get("id")) is int and item["id"] > 0]
    command = max(
        (item for item in comments if _queue_command(str(item.get("body") or ""), queue)),
        key=lambda item: item["id"], default={},
    )
    latest = max(_comments_by(comments, "mergify[bot]"), key=lambda item: (_updated_at(item), item["id"]), default={})
    body = str(latest.get("body") or "")
    payload = _departure_payload(body, queue)
    if not command or not payload:
        return None

    url = f"https://github.com/{repo}/pull/{number}#issuecomment-"
    lines = body.splitlines()
    entered = next((line for line in lines if line.startswith("- ✅ **Entered the queue** ")), "")
    source = re.search(r"^- 🚫 \*\*Left the queue\*\* [^\n]+ · `([0-9a-f]{7,40})`$", body, re.M)
    if f"]({url}{command['id']})" not in entered or not source or not head.startswith(source[1]):
        return None
    markers = [line for line in str(command.get("body") or "").splitlines()
               if line.startswith(f"<!-- {REQUEST} ")]
    if markers and _marker(REQUEST, head, queue) not in markers:
        return None

    reason = payload.get("dequeue_reason")
    checks = reason.get("checks") if isinstance(reason, dict) else None
    names = tuple(name for name in checks if isinstance(name, str) and name) if isinstance(checks, list) else ()
    attribution = next((kind for prefix, kind in (
        ("**The failure is in this pull request.** It failed tested alone on top of ", "isolated"),
        ("**Not proven to be this pull request's failure.** It failed on top of the pull requests ahead of it, and was not tested alone.", "stacked"),
    ) if any(line.startswith(prefix) for line in lines)), "")
    if isinstance(reason, dict) and reason.get("code") == "CONFLICT_WITH_BASE_BRANCH":
        attribution = "base_conflict"
    return Departure(f"{url}{latest['id']}", names, attribution)


def request_status(repo: str, number: int, head: str, queue: str) -> tuple[bool, bool, str | None]:
    login, requested, confirmed = github.login(token=""), False, False
    requested_at = ""
    comments = github._paged(f"repos/{repo}/issues/{number}/comments")
    for item in _comments_by(comments, login):
        body = str(item.get("body") or "")
        lines, requeue = body.splitlines(), _queue_command(body, queue)
        if _marker(REQUEST, head, queue) in lines or (requeue and confirmed):
            requested, requested_at = True, str(item.get("created_at") or "")
        confirmed = _marker(CONFIRMED, head, queue) in lines or (confirmed and not requeue)
    report_id = _admission_report(comments, requested_at)
    report = f"https://github.com/{repo}/pull/{number}#issuecomment-{report_id}" if report_id else None
    return requested, confirmed, report


def markers(repo: str, number: int, head: str, queue: str) -> tuple[bool, bool]:
    requested, confirmed, _ = request_status(repo, number, head, queue)
    return requested, confirmed


def request(repo: str, number: int, head: str, queue: str) -> None:
    body = f"@mergifyio queue\n\n{_marker(REQUEST, head, queue)}"  # Mergify deprecates naming the queue
    try:
        github._api(f"repos/{repo}/issues/{number}/comments", {"body": body})
    except github.GhUnavailable:
        raise
    except (github.GhError, github.ProcTimeout, OSError):
        if not markers(repo, number, head, queue)[0]:
            raise


def mark_confirmed(repo: str, number: int, head: str, queue: str) -> None:
    github._api(f"repos/{repo}/issues/{number}/comments",
                {"body": _marker(CONFIRMED, head, queue)})
