"""Small fail-closed adapter for Mergify queue membership and command requests."""

from __future__ import annotations

import base64
import http.client
import json
import os
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import github
from .types import LoopZeroError

API = "https://api.mergify.com/v1"
REQUEST = "loopzero:mergify-request"
CONFIRMED = "loopzero:mergify-confirmed"


class MergifyError(LoopZeroError):
    """Authentication, transport, or response failure from Mergify."""


@dataclass(frozen=True)
class Membership:
    queue: str
    queued_at: str
    position: int


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


def _open(request: urllib.request.Request):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=30)


def api_get(repo: str, suffix: str) -> object | None:
    """GET one documented endpoint below this repository's fixed Mergify API path."""
    if not suffix.startswith("/") or "://" in suffix or ".." in suffix:
        raise ValueError("Mergify API suffix must be a repository-relative absolute path")
    owner, name = (quote(part, safe="") for part in repo.split("/", 1))
    endpoint = f"{API}/repos/{owner}/{name}{suffix}"
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "loopzero",
                 "Authorization": f"Bearer {_credential()}"},
    )
    for delay in (2.0, 5.0, None):  # reads retry server errors, throttling and dropped reads
        try:
            with _open(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if delay is None or not (exc.code >= 500 or exc.code == 429):
                raise MergifyError(f"Mergify queue lookup failed with HTTP {exc.code}") from exc
        except (OSError, ValueError, http.client.IncompleteRead) as exc:
            if delay is None:
                raise MergifyError("Mergify queue lookup returned no valid JSON") from exc
        _retry_sleep(delay)


def membership(repo: str, number: int, queue: str) -> Membership | None:
    data = api_get(repo, f"/merge-queue/pull/{number}")
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
        return Membership(actual, data["queued_at"], data["position"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MergifyError("invalid Mergify queue membership response") from exc


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
        rules = document["queue_rules"]
        if (mode.get("mode") != "serial" or type(parallel) is not int or parallel <= 1
                or not isinstance(rules, list)
                or sum(isinstance(rule, dict) and rule.get("name") == queue for rule in rules) != 1):
            return False
    except (KeyError, TypeError, ValueError, yaml.YAMLError, RecursionError):
        return False
    # File identity, not main's commit: unrelated merges must not invalidate this proof.
    current = github.api_get(path)
    metadata = github.api_get(repo_path)
    return (isinstance(current, dict) and current.get("sha") == blob["sha"]
            and isinstance(metadata, dict) and metadata.get("default_branch") == branch)


def configured(repo: str, branch: str, queue: str) -> bool:
    """Require live queue identity and stable canonical speculative configuration."""
    status_data = api_get(repo, f"/merge-queue/status?branch={quote(branch, safe='')}")
    if status_data is None:
        return False
    rules_data = api_get(repo, "/queues/configuration")
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
    owner, name = (quote(part, safe="") for part in repo.split("/", 1))
    endpoint = f"{API}/repos/{owner}/{name}/merge-queue/pull/{number}/dequeue"
    request = urllib.request.Request(
        endpoint, method="POST",
        headers={"Accept": "application/json", "User-Agent": "loopzero",
                 "Authorization": f"Bearer {_credential()}"},
    )
    try:
        with _open(request):
            return
    except urllib.error.HTTPError as exc:
        raise MergifyError(f"Mergify dequeue failed with HTTP {exc.code}") from exc
    except OSError as exc:
        raise MergifyError("Mergify dequeue transport failed") from exc


def _marker(kind: str, head: str, queue: str) -> str:
    return f"<!-- {kind} head={head} queue={queue} -->"


def markers(repo: str, number: int, head: str, queue: str) -> tuple[bool, bool]:
    login = github.login()
    data = github._paged(f"repos/{repo}/issues/{number}/comments")
    bodies = [
        str(item.get("body") or "").splitlines()
        for item in data
        if isinstance(item, dict) and (item.get("user") or {}).get("login") == login
    ]
    request = _marker(REQUEST, head, queue)
    confirmed = _marker(CONFIRMED, head, queue)
    return (any(request in lines for lines in bodies), any(confirmed in lines for lines in bodies))


def request(repo: str, number: int, head: str, queue: str) -> None:
    body = f"@mergifyio queue {queue}\n\n{_marker(REQUEST, head, queue)}"
    github._api(f"repos/{repo}/issues/{number}/comments", {"body": body})


def mark_confirmed(repo: str, number: int, head: str, queue: str) -> None:
    github._api(f"repos/{repo}/issues/{number}/comments",
                {"body": _marker(CONFIRMED, head, queue)})
