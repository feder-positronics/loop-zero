from __future__ import annotations

import base64
import http.client
import io
import json
import signal
import ssl
import subprocess
import sys
import traceback
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from loopzero import github, mergify


def response(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())



@pytest.mark.parametrize(("operation", "args"), [
    (mergify.api_get, ("acme/widgets", "/merge-queue/status")),
    (mergify.dequeue, ("acme/widgets", 7)),
])
def test_malformed_authorization_is_not_logged(monkeypatch, operation, args):
    fake_key = "synthetic-mergify-key"
    monkeypatch.setenv("MERGIFY_API_KEY", fake_key + "\nsecond-line")
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.setattr(mergify, "_retry_sleep", lambda _delay: None)

    def reject_network(_connection):
        pytest.fail("invalid Authorization header reached the network")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", reject_network)
    with pytest.raises(mergify.MergifyError, match="invalid URL or header") as failure:
        operation(*args)
    assert fake_key not in "".join(traceback.format_exception(failure.value))


@pytest.fixture
def request_log(monkeypatch):
    monkeypatch.setenv("MERGIFY_API_KEY", "secret")
    seen = []

    def install(payload=None, *, status=None):
        def open_(request):
            seen.append(request)
            if status:
                raise urllib.error.HTTPError(request.full_url, status, "test response", {}, None)
            return response(payload)
        monkeypatch.setattr(mergify, "_open", open_)
        return seen

    return install


@pytest.mark.parametrize("status", [None, 404, 403])
def test_membership_uses_bearer_and_maps_only_404_to_absent(request_log, status):
    seen = request_log({"number": 7, "queue_rule_name": "main", "queued_at": "now", "position": 2}, status=status)
    if status == 403:
        with pytest.raises(mergify.MergifyError, match="HTTP 403"):
            mergify.membership("acme/widgets", 7, "main")
        return
    assert mergify.membership("acme/widgets", 7, "main") == (None if status == 404 else mergify.Membership())
    assert seen[0].full_url == "https://api.mergify.com/v1/repos/acme/widgets/merge-queue/pull/7"
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert seen[0].headers["User-agent"] == "loopzero"


@pytest.mark.parametrize("path", ["https://example.invalid/x", "/../other"])
def test_api_get_rejects_paths_outside_repository(path):
    with pytest.raises(ValueError, match="repository-relative"):
        mergify.api_get("acme/widgets", path)


def test_dequeue_posts_to_documented_endpoint(request_log):
    seen = request_log(status=503)  # a transient failure must not repeat the write
    with pytest.raises(mergify.MergifyError):
        mergify.dequeue("acme/widgets", 7)
    assert (len(seen), seen[0].method, seen[0].full_url.endswith("/pull/7/dequeue")) == (1, "POST", True)


@pytest.fixture
def queue_api(monkeypatch):
    def github_config(text, *, default="main", changed=False, file_overrides=None, queues=True, inplace=True):
        rows = [{"name": "main", "config": {"allow_inplace_checks": inplace}}] if queues else []
        monkeypatch.setattr(mergify, "api_get", lambda _repo, suffix, *_: (
            {"batches": [], "mode": "serial"} if suffix.startswith("/merge-queue/status")
            else {"configuration": rows}
        ))
        reads = 0
        def get(path):
            nonlocal reads
            if path == "repos/acme/widgets":
                return {"default_branch": default}
            assert path == "repos/acme/widgets/contents/.mergify.yml?ref=refs%2Fheads%2Fmain"
            reads += 1
            return {"type": "file", "encoding": "base64", "sha": str(reads if changed else 1)*40,
                    "content": base64.b64encode(text.encode()).decode(), **(file_overrides or {})}
        monkeypatch.setattr(mergify.github, "api_get", get)
    return github_config


SAFE_CONFIG = "merge_queue: {mode: serial, max_parallel_checks: 3}\nqueue_rules: [{name: main}]\n"
ONE_CHECK = SAFE_CONFIG.replace("checks: 3", "checks: 1")  # in place unless batches are explicit


UNSAFE_CONFIGS = [
    SAFE_CONFIG.replace("checks: 3", f"checks: {value}")
    for value in ["1", "0", "-1", "true", '"3"', "null"]
] + [
    SAFE_CONFIG.replace("mode: serial", "mode: parallel"),
    SAFE_CONFIG.replace("name: main", "name: other"),
    SAFE_CONFIG + "extends: [other/repo]\n",
    SAFE_CONFIG + "merge_queue: {max_parallel_checks: 1}\n",
    "[]", "!!python/object:object {}", "merge_queue: [",
] + [ONE_CHECK.replace("main}", r) for r in ["main, batch_size: 1}", "main, batch_size: true}",
     'main, batch_size: "8"}', "main, batch_size: null}", "main}, {name: o, batch_size: 8}"]]


@pytest.mark.parametrize("text,options,accepted", [
    (SAFE_CONFIG, {}, True),
    (ONE_CHECK.replace("main}", "main, batch_size: 8}"), {}, True),
    *((text, {}, False) for text in UNSAFE_CONFIGS),
    (SAFE_CONFIG, {"default": "other"}, False),
    (SAFE_CONFIG, {"changed": True}, False),
    (SAFE_CONFIG, {"queues": False}, False),
    (ONE_CHECK, {"inplace": False}, False),
    *((SAFE_CONFIG, {"file_overrides": {key: value}}, False)
      for key, value in (("encoding", "none"), ("type", "symlink"), ("sha", None),
                         ("sha", "z" * 40), ("content", None), ("content", "!"))),
])
def test_configured_validates_canonical_github_config(queue_api, text, options, accepted):
    queue_api(text, **options)
    assert mergify.configured("acme/widgets", "main", "main") == accepted


def test_markers_only_accept_exact_lines_from_current_login(monkeypatch):
    head, marker = "a" * 40, f"<!-- loopzero:mergify-confirmed head={'a' * 40} queue=main -->"
    rows = [("trusted", "@mergifyio queue main"), ("attacker", marker),  # early queue: no request
            ("trusted", f"prefix {marker}"), ("trusted", marker), ("attacker", "@mergifyio requeue")]
    monkeypatch.setattr(mergify.github, "login", lambda token=None: "trusted" if token == "" else "app[bot]")
    for later, requeued in [("", False), ("@mergifyio queued", False), ("@mergifyio requeued", False),
            ("@mergifyio queue dev", False), ("@Mergifyio queue main", True), ("@mergify requeue", True)]:
        comments = [{"body": b, "user": {"login": who}} for who, b in [*rows, ("trusted", later)]]
        monkeypatch.setattr(mergify.github, "_paged", lambda *_, c=comments: c)
        assert mergify.markers("acme/widgets", 7, head, "main") == (requeued, not requeued), later


@pytest.mark.parametrize(("writer", "body"), [
    (mergify.request, "@mergifyio queue\n\n<!-- loopzero:mergify-request head=abc queue=main -->"),
    (mergify.mark_confirmed, "<!-- loopzero:mergify-confirmed head=abc queue=main -->"),
])
def test_comment_writes_use_github_rest_api(monkeypatch, writer, body):
    calls = []

    def run(argv, **_kwargs):
        input_index = argv.index("--input") + 1
        with open(argv[input_index], encoding="utf-8") as handle:
            payload = json.load(handle)
        calls.append((argv[1:input_index] + argv[input_index + 1:], payload))
        return type("Done", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mergify.github, "run", run)
    writer("acme/widgets", 7, "abc", "main")

    assert calls == [([
        "api", "repos/acme/widgets/issues/7/comments", "--method", "POST", "--input", "--include",
    ], {"body": body})]


def test_credential_file_must_be_private(tmp_path, monkeypatch):
    monkeypatch.delenv("MERGIFY_API_KEY", raising=False)
    key = tmp_path / "api-key"
    key.write_text("secret\n")
    key.chmod(0o644)
    with pytest.raises(mergify.MergifyError, match="unsafe"):
        mergify._credential(key)
    key.chmod(0o600)
    assert mergify._credential(key) == "secret"


@pytest.mark.parametrize("field,value", [("number", "7"), ("position", None), ("queued_at", None)])
def test_malformed_membership_never_confirms(request_log, field, value):
    request_log({"number": 7, "position": 1, "queued_at": "now", "queue_rule_name": "main", field: value})
    with pytest.raises(mergify.MergifyError):
        mergify.membership("acme/widgets", 7, "main")


def test_redirect_is_refused_before_credentials_can_leave_origin():
    with pytest.raises(mergify.MergifyError, match="redirect"):
        mergify._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid/")


def test_hosted_import_does_not_load_yaml():
    result = subprocess.run([sys.executable, "-c", ("import sys; "
                             "sys.modules['yaml'] = None; "
                             "from loopzero import hosted, mergify; "
                             "assert callable(mergify.api_get)")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure,status,budget,wait", [
    ("http", 503, None, 2), ("reset", None, None, 2), ("truncated", None, None, 2),
    ("http", 429, 100, 40), ("http", 429, None, None), ("http", 403, 20, None), ("http", 429, 20, None),
    ("DNS lookup failed", None, None, 2), (ssl.SSLCertVerificationError("certificate verification failed"), None, None, None),
])
def test_api_get_retries_and_classifies_failures(monkeypatch, failure, status, budget, wait):
    calls, sleeps = [], []
    body_errors = {"reset": ConnectionResetError(), "truncated": http.client.IncompleteRead(b"{")}

    class Body(io.BytesIO):
        def read(self, *args):
            if len(calls) == 1 and failure in body_errors:
                raise body_errors[failure]
            return super().read(*args)

    def fake_open(request):
        calls.append((request.get_method(), sum(sleeps)))
        if status in {403, 429} or (status == 503 and len(calls) == 1):
            raise urllib.error.HTTPError(request.full_url, status, "x", {"Retry-After": "40"} if status < 500 else {}, None)
        if not status and failure not in body_errors:
            raise urllib.error.URLError(failure)
        return Body(b"{}")

    monkeypatch.setattr(mergify, "_open", fake_open)
    monkeypatch.setattr(mergify, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(mergify, "_credential", lambda: "k")
    if status in {403, 429}:
        with pytest.raises(mergify.MergifyError, match="Retry-After: 40s" + ("; retry delay exceeds" if wait is None else "")):
            mergify.api_get("acme/widgets", "/merge-queue/status", None if budget is None else mergify.time.monotonic() + budget)
    elif status == 503 or failure in body_errors:
        assert mergify.api_get("acme/widgets", "/merge-queue/status") == {}
    else:
        with pytest.raises(mergify.MergifyError, match="transport failed") as info:
            mergify.api_get("acme/widgets", "/merge-queue/status")
        assert str(failure) in str(info.value)
    assert calls == ([("GET", 0)] if wait is None else [("GET", 0), ("GET", wait)])
    assert sleeps == ([] if wait is None else [wait])


@pytest.mark.parametrize("phase", ["headers", "body", "cleanup"])
def test_request_deadline(monkeypatch, phase):
    handler, setitimer = signal.getsignal(signal.SIGALRM), signal.setitimer

    def open_(*args, **kwargs):
        if phase == "headers":
            try:
                signal.raise_signal(signal.SIGALRM)
            except OSError:
                pytest.fail("address fallback would swallow the deadline")
        return io.BytesIO(b"{}")

    def arm(timer, seconds):
        setitimer(timer, seconds)
        if seconds == 0 and phase == "cleanup":
            signal.raise_signal(signal.SIGALRM)

    monkeypatch.setattr(mergify.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_))
    monkeypatch.setattr(signal, "setitimer", arm)
    with (
        pytest.raises(TimeoutError, match="Mergify HTTP request timed out"),
        mergify._open(urllib.request.Request("https://example.invalid")),
    ):
        if phase == "body":
            assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 30
            signal.raise_signal(signal.SIGALRM)
    assert signal.getsignal(signal.SIGALRM) is handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


@pytest.mark.parametrize("response", [
    'HTTP/2 429 Too Many Requests\n\n{}',
    'HTTP/2 403 Forbidden\nX-RateLimit-Remaining: 0\n\n{}',
    'HTTP/2 403 Forbidden\n\n{"message":"API rate limit exceeded"}',
])
def test_rate_limited_queue_post_does_not_reconcile(monkeypatch, response):
    monkeypatch.setattr(github, "run", lambda *_a, **_k: SimpleNamespace(
        exit_code=1, stdout=response, stderr="",
    ))
    monkeypatch.setattr(mergify, "markers", lambda *_: pytest.fail("read markers after rate-limited POST"))
    with pytest.raises(github.GhUnavailable) as exc:
        mergify.request("acme/widgets", 7, "a" * 40, "main")
    assert exc.value.confirmed_rate_limit
