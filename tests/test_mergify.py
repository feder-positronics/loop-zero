from __future__ import annotations

import base64
import http.client
import http.server
import io
import json
import signal
import ssl
import subprocess
import sys
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.error
import urllib.request

import pytest

from loopzero import github, mergify
from tests.test_github import gh as gh



@pytest.mark.parametrize(("operation", "args"), [
    (mergify.api_get, ("acme/widgets", "/merge-queue/status")),
    (mergify.dequeue, ("acme/widgets", 7)),
])
def test_malformed_authorization_is_not_logged(monkeypatch, operation, args):
    fake_key = "synthetic-mergify-key"
    monkeypatch.setenv("MERGIFY_API_KEY", fake_key + "\nsecond-line")
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.setattr(mergify, "_retry_sleep", lambda _delay: None)

    connect = Mock(side_effect=AssertionError("invalid Authorization header reached the network"))
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    with pytest.raises(mergify.MergifyError, match="invalid URL or header") as failure:
        operation(*args)
    connect.assert_not_called()
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
            return io.BytesIO(json.dumps(payload).encode())

        monkeypatch.setattr(mergify, "_open", open_)
        return seen

    return install


@pytest.mark.parametrize("status,fields,diagnostic", [
    (None, {}, None), (404, {}, None), (403, {}, "HTTP 403"),
    (None, {"number": "7"}, ""), (None, {"position": None}, ""),
    (None, {"queued_at": None}, ""), (None, {"number": 8}, ""),
    (None, {"queue_rule_name": "other"}, "PR #7 is in Mergify queue 'other', expected 'main'"),
])
def test_membership_validates_response_and_sends_bearer(request_log, status, fields, diagnostic):
    payload = {"number": 7, "queue_rule_name": "main", "queued_at": "now", "position": 2, **fields}
    seen = request_log(payload, status=status)
    if diagnostic is not None:
        with pytest.raises(mergify.MergifyError, match=diagnostic or None):
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
    def github_config(text, *, default="main", changed=False, file_overrides=None, queues=True, inplace=True, mode="serial"):
        rows = [{"name": "main", "config": {"allow_inplace_checks": inplace}}] if queues else []
        monkeypatch.setattr(mergify, "api_get", lambda _repo, suffix, *_: (
            {"batches": [], "mode": mode} if suffix.startswith("/merge-queue/status")
            else {"configuration": rows}
        ))
        calls = []

        def get(path):
            calls.append(path)
            if path == "repos/acme/widgets":
                return {"default_branch": default}
            assert path == "repos/acme/widgets/contents/.mergify.yml?ref=refs%2Fheads%2Fmain"
            return {"type": "file", "encoding": "base64", "sha": str(len(calls) if changed else 1)*40,
                    "content": base64.b64encode(text.encode()).decode(), **(file_overrides or {})}
        monkeypatch.setattr(mergify.github, "api_get", get)
        return calls
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
    (SAFE_CONFIG, {}, True), (SAFE_CONFIG, {"mode": "parallel"}, False),
    (ONE_CHECK.replace("main}", "main, batch_size: 8}"), {}, True),
    (ONE_CHECK.replace("{name: main}", "{<<: {name: other, batch_size: 1}, name: main, batch_size: 8}"), {}, True),
    (ONE_CHECK.replace("{name: main}", "{<<: [&defaults {<<: {name: base}, name: other, batch_size: 1}, *defaults], "
                       "name: main, batch_size: 8}"), {}, True),
    (ONE_CHECK.replace("{name: main}", "{<<: {name: other, name: main, batch_size: 8}}"), {}, False),
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
    calls = queue_api(text, **options)
    assert mergify.configured("acme/widgets", "main", "main") == accepted
    if accepted:
        config = "repos/acme/widgets/contents/.mergify.yml?ref=refs%2Fheads%2Fmain"
        assert calls == [config, config, "repos/acme/widgets"]


def test_queue_attempt_trusts_same_head_request_from_another_writer(monkeypatch):
    head = "a" * 40
    marker = f"<!-- loopzero:mergify-confirmed head={head} queue=main -->"
    rows = [("trusted", "@mergifyio queue main"), ("attacker", marker),  # early queue: no request
            ("trusted", f"prefix {marker}"), ("trusted", marker), ("attacker", "@mergifyio requeue")]
    monkeypatch.setattr(mergify.github, "login", lambda token=None: "trusted" if token == "" else "app[bot]")
    request = f"@mergifyio queue\n\n<!-- loopzero:mergify-request head={head} queue=main -->"
    monkeypatch.setattr(mergify.github, "api_get", lambda endpoint: {"permission": "write" if endpoint.endswith("/writer/permission") else "read"})
    for actor, later, expected in [
        *(("trusted", body, (False, True)) for body in ("", "@mergifyio queued", "@mergifyio requeued", "@mergifyio queue dev")),
        ("trusted", "@Mergifyio queue main", (True, False)), ("trusted", "@mergify requeue", (True, False)),
        ("writer", request, (True, False)), ("attacker", request, (False, True)),
    ]:
        comments = [{"body": b, "user": {"login": who}} for who, b in [*rows, (actor, later)]]
        monkeypatch.setattr(mergify.github, "paged", lambda *_, c=comments: c)
        assert mergify.queue_attempt("acme/widgets", 7, head, "main") == mergify.QueueAttempt(*expected), (actor, later)


@pytest.mark.parametrize(("writer", "body"), [
    (mergify.request, "@mergifyio queue\n\n<!-- loopzero:mergify-request head=abc queue=main -->"),
    (mergify.mark_confirmed, "<!-- loopzero:mergify-confirmed head=abc queue=main -->"),
])
def test_comment_writes_use_github_rest_api(gh, writer, body):
    endpoint = "repos/acme/widgets/issues/7/comments"
    gh.respond(f"api {endpoint} --method POST", {})
    writer("acme/widgets", 7, "abc", "main")
    call, = gh.calls
    assert call["argv"][:5] == ["api", endpoint, "--method", "POST", "--input"]
    assert call["argv"][6:] == ["--include"]
    assert json.loads(call["input_files"]["--input"]) == {"body": body}


@pytest.mark.parametrize("unsafe,diagnostic", [
    ("file", r"unsafe Mergify credential file permissions: .*expected mode 0600"),
    ("directory", r"unsafe Mergify credential directory permissions: .*expected mode 0700"),
    ("symlink", "unable to open Mergify credential file"),
    ("parent_file", "unsafe Mergify credential directory permissions"),
    ("not_regular", "unsafe Mergify credential file permissions"),
    ("foreign_owner", "unsafe Mergify credential file permissions"),
])
def test_credential_file_must_be_private(tmp_path, monkeypatch, unsafe, diagnostic):
    monkeypatch.delenv("MERGIFY_API_KEY", raising=False)
    key = tmp_path / "api-key"
    key.write_text("secret\n")
    key.chmod(0o600)
    assert mergify._credential(key) == "secret"
    if unsafe in {"file", "directory"}:
        (key if unsafe == "file" else tmp_path).chmod(0o644 if unsafe == "file" else 0o755)
    elif unsafe == "parent_file":
        parent = tmp_path / "not-a-directory"
        parent.write_text("")
        parent.chmod(0o700)
        key = parent / "api-key"
    elif unsafe == "not_regular":
        key.unlink()
        key.mkdir(mode=0o600)
    elif unsafe == "foreign_owner":
        fstat = mergify.os.fstat

        def foreign(fd):
            fields = list(fstat(fd))
            fields[4] = mergify.os.getuid() + 1
            return mergify.os.stat_result(fields)

        monkeypatch.setattr(mergify.os, "fstat", foreign)
    else:
        target = key.with_name("real-key")
        key.rename(target)
        key.symlink_to(target)
    with pytest.raises(mergify.MergifyError, match=diagnostic):
        mergify._credential(key)


def test_redirect_is_refused_before_credentials_can_leave_origin(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    followed = []

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/elsewhere":
                followed.append(self.headers.get("Authorization"))
            self.send_response_only(302 if self.path == "/" else 200)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirecting) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/",
                                         headers={"Authorization": "Bearer secret"})
        try:
            with pytest.raises(mergify.MergifyError, match="redirect"), mergify._open(request, timeout=10):
                pass
        finally:
            server.shutdown()
    assert followed == []


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

    def fake_open(request):
        calls.append((request.get_method(), sum(sleeps)))
        if status in {403, 429} or (status == 503 and len(calls) == 1):
            raise urllib.error.HTTPError(request.full_url, status, "x", {"Retry-After": "40"} if status < 500 else {}, None)
        if not status and failure not in body_errors:
            raise urllib.error.URLError(failure)
        body = io.BytesIO(b"{}")
        if len(calls) == 1 and failure in body_errors:
            body.read = Mock(side_effect=body_errors[failure])
        return body

    monkeypatch.setattr(mergify, "_open", fake_open)
    monkeypatch.setattr(mergify, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(mergify, "_credential", lambda: "k")
    deadline = None if budget is None else mergify.time.monotonic() + budget
    if status == 503 or failure in body_errors:
        assert mergify.api_get("acme/widgets", "/merge-queue/status", deadline) == {}
    else:
        diagnostic = "transport failed" if status is None else "Retry-After: 40s"
        if status and wait is None:
            diagnostic += "; retry delay exceeds"
        with pytest.raises(mergify.MergifyError, match=diagnostic) as info:
            mergify.api_get("acme/widgets", "/merge-queue/status", deadline)
        if status is None:
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
    monkeypatch.setattr(mergify, "queue_attempt", lambda *_: pytest.fail("read queue attempt after rate-limited POST"))
    with pytest.raises(github.GhUnavailable) as exc:
        mergify.request("acme/widgets", 7, "a" * 40, "main")
    assert exc.value.confirmed_rate_limit


def test_failed_job_diagnostics_are_bounded_and_keep_links(monkeypatch):
    repo = "acme/widgets"
    base = f"https://github.com/{repo}/actions/runs/99/job/"
    report = "\n".join(f"- ❌ [`{name}`]({base}{job})"
                       for job, name in enumerate(("gate", "unit", "static", "backend / validation"), 1))
    report += "\n- ❌ [`foreign`](https://github.com/other/project/actions/runs/99/job/5)"
    calls = []

    def logs(argv, **options):
        job = argv[2].split("/")[-2]
        calls.append(job)
        assert argv[1:] == ["api", f"repos/{repo}/actions/jobs/{job}/logs",
                            "--hostname", "github.com", "--allow-escape-sequences"]
        assert options["tail_chars"] == 256 * 1024 and 0 < options["timeout"] <= 5
        if job in {"1", "2"}:
            output = "credential in remote error" if job == "1" else "ordinary output"
            return SimpleNamespace(exit_code=int(job == "1"), stdout=output)
        return SimpleNamespace(exit_code=0, stdout="\n".join([
            "\x1b[31m FAIL  frontend.test.ts > suite > test\x1b[0m",
            "  1) [chromium] \u203a tests/example.spec.ts:2:1 \u203a summary",
            *(f"2026-11-26T11:47:24.4155165Z ERROR tests/test_example.py::test_failure[{i}] - detail"
              for i in [0, *range(12)]),
        ]))

    monkeypatch.setattr(github, "run", logs)
    result = mergify.Departure("report", (), "isolated", report).jobs(repo)
    assert calls == ["4", "1", "2"]
    assert all(f"{base}{job}" in result for job in range(1, 5))
    assert "foreign" not in result and "credential in remote error" not in result
    assert "log limit reached" in result and "request failed (exit 1)" in result and "no test failures" in result
    assert "frontend.test.ts > suite > test" in result and "[chromium] \u203a tests/example.spec.ts" in result
    assert result.count("test_failure[0]") == 1
    assert "test_failure[7]" in result and "test_failure[8]" not in result
    assert "\x1b" not in result
