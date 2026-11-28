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
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import github
from .types import LoopZeroError, is_sha

API = "https://api.mergify.com/v1"
REQUEST = "loopzero:mergify-request"
CONFIRMED = "loopzero:mergify-confirmed"


class MergifyError(LoopZeroError):
    """Authentication, transport, or response failure from Mergify."""


class MergifyUnavailable(MergifyError):
    """Transient Mergify lookup failure after retry or budget exhaustion."""


@dataclass(frozen=True)
class Membership:
    candidate_number: int | None = None
    unmet_conditions: tuple[str, ...] = ()


_MODES = " (need directory 0700 and file 0600; see SETUP.md)"


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
        raise MergifyError(f"unsafe Mergify credential directory permissions: {path.parent}; expected mode 0700 and user ownership{_MODES}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise MergifyError(f"unable to open Mergify credential file: {path}{_MODES}") from exc
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(fd)
        raise MergifyError(f"unsafe Mergify credential file permissions: {path}; expected mode 0600 and user ownership{_MODES}")
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
    timeout = github.read_timeout(timeout)
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


def api_get(repo: str, suffix: str, deadline: float | None = None) -> object | None:
    """GET one documented endpoint below this repository's fixed Mergify API path."""
    if not suffix.startswith("/") or "://" in suffix or ".." in suffix:
        raise ValueError("Mergify API suffix must be a repository-relative absolute path")
    with github.bounded_reads(deadline, token=None, retry=True) if deadline is not None else nullcontext():
        return _read_queue(_api_request(repo, suffix), deadline)


def _read_queue(request: urllib.request.Request, deadline: float | None) -> object | None:
    for retry in (True, False):
        wait = None
        try:
            with _open(request) as response:
                payload = json.load(response)
            github.read_timeout(0)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            wait = github.retry_after(exc.headers.get("Retry-After", "")) if exc.headers else None
            detail = f"Mergify queue lookup failed with HTTP {exc.code}" + (f"; Retry-After: {wait:g}s" if wait is not None else "")
            transient = exc.code >= 500 or exc.code == 429 or (exc.code == 403 and wait is not None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail, transient = "Mergify queue lookup returned no valid JSON", True
        except (OSError, http.client.IncompleteRead) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            detail = f"Mergify queue transport failed: {reason}"
            transient = not isinstance(reason, ssl.SSLCertVerificationError)
        if not transient:
            raise MergifyError(detail)
        if not retry:
            break
        delay = max(2.0, wait or 0.0)
        remaining = github.read_timeout() if deadline is not None else github.read_timeout(5.0)
        if delay >= remaining:
            if wait is None:
                _retry_sleep(remaining)
                github.read_timeout(0)
            raise MergifyUnavailable(f"{detail}; retry delay exceeds the wait budget")
        _retry_sleep(delay)
        github.read_timeout(0)
    raise MergifyUnavailable(detail)


def _unmet_conditions(node: object) -> tuple[str, ...]:
    if not isinstance(node, dict) or node.get("match") is not False:
        return ()
    children = node.get("subconditions")
    if isinstance(children, list) and children:
        labels = tuple(label for child in children for label in _unmet_conditions(child))
        return (" | ".join(labels),) if labels and node.get("label") == "any of" else labels
    label = node.get("label")
    return (label,) if isinstance(label, str) and label.isprintable() else ()


def membership(repo: str, number: int, queue: str, deadline: float | None = None) -> Membership | None:
    data = api_get(repo, f"/merge-queue/pull/{number}", deadline)
    if data is None:
        return None
    try:
        if type(data["number"]) is not int or data["number"] != number:
            raise ValueError("pull request number mismatch")
        actual = data["queue_rule_name"]
        if not isinstance(actual, str) or not actual:
            raise ValueError("invalid queue name")
        if actual != queue:
            raise MergifyError(f"PR #{number} is in Mergify queue {actual!r}, expected {queue!r}")
        if (not isinstance(data["queued_at"], str) or not data["queued_at"]
                or type(data["position"]) is not int or data["position"] < 0):
            raise ValueError("invalid queue position or admission time")
        check = data.get("mergeability_check")
        if not isinstance(check, dict):
            return Membership()
        candidate = check.get("queue_pull_request_number")
        return Membership(candidate if type(candidate) is int and candidate > 0 else None,
                          _unmet_conditions(check.get("conditions_evaluation")))
    except (KeyError, TypeError, ValueError) as exc:
        detail = f": {exc}" if type(exc) is ValueError else ""
        raise MergifyError(f"invalid Mergify queue membership response{detail}") from exc


def _preserves_source_heads(repo: str, branch: str, queue: str) -> bool:
    """Read the vendor's canonical input, not a deprecated raw queue-rule default."""
    import yaml

    class _ConfigLoader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self.checked_mappings = set()

        def flatten_mapping(self, node):
            if node not in self.checked_mappings:
                self.checked_mappings.add(node)
                merge_tag, value_tag = "tag:yaml.org,2002:merge", "tag:yaml.org,2002:value"
                keys = [object if key.tag == merge_tag else self.construct_scalar(key) if key.tag == value_tag
                        else self.construct_object(key, deep=True)
                        for key, _ in node.value]
                if len(keys) != len(set(keys)):
                    raise yaml.YAMLError("duplicate configuration key")
            super().flatten_mapping(node)

    repo_path = f"repos/{repo}"
    path = f"{repo_path}/contents/.mergify.yml?ref={quote('refs/heads/' + branch, safe='')}"
    blob = github.api_get(path)
    try:
        if (blob["type"] != "file" or blob["encoding"] != "base64"
                or not is_sha(blob["sha"])
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
    return (comment for comment in comments
            if isinstance(user := comment.get("user"), dict) and user.get("login") == login)


def _updated_at(comment: dict) -> str:
    return str(comment.get("updated_at") or "")


def _queue_command(body: str, queue: str) -> bool:
    words = body.partition("\n")[0].split()
    return (len(words) in (2, 3) and words[0].lower() in ("@mergify", "@mergifyio")
            and words[1].lower() in ("queue", "requeue") and words[2:] in ([], [queue]))


@dataclass(frozen=True)
class Departure:
    url: str
    checks: tuple[str, ...]
    attribution: str
    failure_report: str | None = None
    reason: str = ""

    def jobs(self, repo: str) -> str:
        if self.failure_report is None:
            return ""
        base = f"https://github.com/{repo}/actions/runs/"
        links = re.findall(rf"^- ❌ \[`([^\n]+?)`\]\({re.escape(base)}([0-9]+/job/([0-9]+))\)",
                           self.failure_report, re.M)
        if not links and (candidates := re.findall(rf"^- ❌ \*\*Checks failed\*\* [^\n]*\(/{re.escape(repo)}/pull/(\d+)\)",
                                                    self.failure_report, re.M)):
            try:
                with github.bounded_reads(time.monotonic() + 10, token=None, retry=True, check_on_exit=False):
                    sha = github.pr_view(repo, int(candidates[-1])).head_sha
                    runs = github.paged(f"repos/{repo}/commits/{sha}/check-runs", "check_runs")
                for run in runs:
                    job = re.fullmatch(rf"{re.escape(base)}([0-9]+/job/([0-9]+))", str(run.get("details_url") or ""))
                    if job and run.get("conclusion") in {"failure", "timed_out", "startup_failure"}:
                        links.append((str(run.get("name") or "Unnamed job"), *job.groups()))
            except (github.GhError, github.ProcTimeout, github.ToolMissing, OSError, ValueError):
                return " Candidate jobs unavailable: GitHub could not read the candidate checks."
        jobs = sorted(dict.fromkeys(links), key=lambda job: " / " not in job[0])
        return " " + " ".join(
            f"{github._redact_tokens(name)!r} ({base}{path}): " + (_job_failure(repo, job_id) if index < 3 else "log limit reached") + "."
            for index, (name, path, job_id) in enumerate(jobs)
        ) if jobs else " Candidate jobs unavailable: no failed GitHub Actions jobs found."


def _job_failure(repo: str, job_id: str) -> str:
    try:
        result = github.job_log(repo, job_id)
        if result.exit_code:
            return f"GitHub log request failed (exit {result.exit_code})"
    except (github.ProcTimeout, github.ToolMissing, OSError, github.ReadDeadline) as exc:
        return f"log unavailable ({type(exc).__name__})"
    log = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))", "", result.stdout)
    headers = re.findall(r"^(?:\d{4}-\d\d-\d\dT\S+Z )? *(?:(?:FAILED|ERROR) ((?=[^\s\[]*(?:::|\.py\b))[^\s\[]++)(\[[^\r\n]*)?(?= - |\r?$)"
                         r"|(?:FAIL +|\d+\) +(?=[^\r\n]*\u203a))([^\r\n]+))", log, re.M)
    names = (github._redact_tokens(name)[:500] for node, suffix, header in headers
             if (name := _pytest_node(node, suffix) if suffix else node or header))
    return ", ".join(repr(name) for name in tuple(dict.fromkeys(names))[:10]) or "no test failures in log tail"


def _pytest_node(node: str, suffix: str) -> str:
    depth = fallback = 0
    for bracket in re.finditer(r"\[|\](?P<boundary>(?= - |$))|\]", suffix):
        depth += 1 if bracket[0] == "[" else -1
        if bracket["boundary"] is not None:
            if depth == 0:
                return node + suffix[:bracket.end()]
            fallback = fallback or bracket.end()
    # Pytest allows literal unmatched brackets in IDs.
    return node + suffix[:fallback] if fallback else ""


def _departure(
    comments: list[dict], repo: str, number: int, head: str, queue: str,
) -> tuple[Departure | None, dict]:
    """Return the current departure or the later command that superseded it."""
    comments = [item for item in comments if type(item.get("id")) is int and item["id"] > 0]
    commands = (item for item in comments if _queue_command(str(item.get("body") or ""), queue))
    command = max(commands, key=lambda item: item["id"], default={})
    latest = max(_comments_by(comments, "mergify[bot]"), key=lambda item: (_updated_at(item), item["id"]), default={})
    body = str(latest.get("body") or "")
    payload = None
    with suppress(json.JSONDecodeError):
        text = body.partition("-*- Mergify Payload -*-\n")[2]
        payload = json.loads(text.partition("\n-*- Mergify Payload End -*-")[0])
    url = f"https://github.com/{repo}/pull/{number}#issuecomment-"
    lines = body.splitlines()
    entered = next((line for line in lines if line.startswith("- ✅ **Entered the queue** ")), "")
    source = re.search(r"^- 🚫 \*\*Left the queue\*\* [^\n]+ · `([0-9a-f]{7,40})`$", body, re.M)
    if (not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("state") != "dequeued"
            or payload.get("queue_rule_name") != queue or not entered or not source or not head.startswith(source[1])):
        return None, {}
    linked = re.search(rf"\]\({re.escape(url)}(\d+)\)", entered)  # absent when a rule or unlinked command admitted
    admitted = next((item for item in comments if linked and item["id"] == int(linked[1])), {})
    markers = [line for line in str(admitted.get("body") or "").splitlines() if line.startswith(f"<!-- {REQUEST} ")]
    if linked and (not admitted or (markers and _marker(REQUEST, head, queue) not in markers)):
        return None, {}
    # Second-precision timestamps: a command in the departure's second may be a new attempt.
    if command and command is not admitted and str(command.get("created_at") or "") >= _updated_at(latest):
        return None, command

    reason = payload["dequeue_reason"] if isinstance(payload.get("dequeue_reason"), dict) else {}
    checks = reason.get("checks")
    names = tuple(name for name in checks if isinstance(name, str) and name) if isinstance(checks, list) else ()
    attribution = next((kind for prefix, kind in (
        ("**The failure is in this pull request.** ", "isolated"),
        ("**Not proven to be this pull request's failure.** ", "stacked"),
    ) if any(line.startswith(prefix) for line in lines)), "")
    if reason.get("code") == "CONFLICT_WITH_BASE_BRANCH" or "> - [ ] `-conflict`" in body:
        attribution = "base_conflict"
    failures = body if reason.get("code") == "CHECKS_FAILED" and attribution in {"isolated", "stacked"} else None
    return Departure(f"{url}{latest['id']}", names, attribution, failures, str(reason.get("label") or "")), {}


def departure_report(repo: str, number: int, head: str, queue: str) -> Departure | None:
    try:
        comments = github.paged(f"repos/{repo}/issues/{number}/comments")
    except github.GhError:
        return None
    return _departure(comments, repo, number, head, queue)[0]


@dataclass(frozen=True)
class QueueAttempt:
    requested: bool = False
    confirmed: bool = False
    departure: Departure | None = None
    admission: str | None = None

    @property
    def was_admitted(self) -> bool:
        return self.confirmed or self.departure is not None


def queue_attempt(repo: str, number: int, head: str, queue: str) -> QueueAttempt:
    login, requested, confirmed = github.login(token=""), False, False
    requested_at = ""
    comments = github.paged(f"repos/{repo}/issues/{number}/comments")
    departure, retry = _departure(comments, repo, number, head, queue)
    for item in _comments_by(comments, login):
        body = str(item.get("body") or "")
        lines, requeue = body.splitlines(), _queue_command(body, queue)
        marked = any(line.startswith(f"<!-- {REQUEST} ") for line in lines)
        if _marker(REQUEST, head, queue) in lines or (not marked and requeue and (confirmed or item is retry)):
            requested, requested_at = True, str(item.get("created_at") or "")
        confirmed = _marker(CONFIRMED, head, queue) in lines or (confirmed and not requeue)
    latest = max(_comments_by(comments, "mergify[bot]"), key=_updated_at, default={})
    report = None
    if (_updated_at(latest) >= requested_at and type(report_id := latest.get("id")) is int and report_id > 0
            and "### ⏳ Waiting for queue conditions" in str(latest.get("body") or "").splitlines()):
        report = f"https://github.com/{repo}/pull/{number}#issuecomment-{report_id}"
    return QueueAttempt(requested, confirmed, departure, report)


def request(repo: str, number: int, head: str, queue: str) -> None:
    body = f"@mergifyio queue\n\n{_marker(REQUEST, head, queue)}"  # Mergify deprecates naming the queue
    try:
        github._api(f"repos/{repo}/issues/{number}/comments", {"body": body})
    except (github.GhError, github.ProcTimeout, OSError) as exc:
        if getattr(exc, "confirmed_rate_limit", False) or not queue_attempt(repo, number, head, queue).requested:
            raise


def mark_confirmed(repo: str, number: int, head: str, queue: str) -> None:
    github._api(f"repos/{repo}/issues/{number}/comments",
                {"body": _marker(CONFIRMED, head, queue)})
