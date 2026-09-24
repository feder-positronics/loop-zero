from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from loopzero import mergify


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def response(payload: object) -> Response:
    return Response(json.dumps(payload).encode())


def test_membership_uses_bearer_and_validates_queue(monkeypatch):
    monkeypatch.setenv("MERGIFY_API_KEY", "secret")
    seen = []

    def open_(request):
        seen.append(request)
        return response({
            "number": 7, "queue_rule_name": "main", "queued_at": "now", "position": 2,
        })

    monkeypatch.setattr(mergify, "_open", open_)
    assert mergify.membership("acme/widgets", 7, "main") == mergify.Membership(
        "main", "now", 2
    )
    assert seen[0].full_url == (
        "https://api.mergify.com/v1/repos/acme/widgets/merge-queue/pull/7"
    )
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert seen[0].headers["User-agent"] == "loopzero"


def test_membership_maps_404_only_to_absent(monkeypatch):
    monkeypatch.setenv("MERGIFY_API_KEY", "secret")

    def missing(request):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(mergify, "_open", missing)
    assert mergify.membership("acme/widgets", 7, "main") is None

    def forbidden(request):
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

    monkeypatch.setattr(mergify, "_open", forbidden)
    with pytest.raises(mergify.MergifyError, match="HTTP 403"):
        mergify.membership("acme/widgets", 7, "main")


def test_api_get_rejects_paths_outside_repository():
    with pytest.raises(ValueError, match="repository-relative"):
        mergify.api_get("acme/widgets", "https://evil.example/x")
    with pytest.raises(ValueError, match="repository-relative"):
        mergify.api_get("acme/widgets", "/../other")


def test_dequeue_posts_to_documented_endpoint(monkeypatch):
    monkeypatch.setenv("MERGIFY_API_KEY", "secret")
    seen = []

    def open_(request):  # a transient failure must not repeat the write
        seen.append(request)
        raise urllib.error.HTTPError(request.full_url, 503, "x", {}, None)

    monkeypatch.setattr(mergify, "_open", open_)
    with pytest.raises(mergify.MergifyError):
        mergify.dequeue("acme/widgets", 7)
    assert (len(seen), seen[0].method, seen[0].full_url.endswith("/pull/7/dequeue")) == (1, "POST", True)


@pytest.fixture
def queue_api(monkeypatch):
    monkeypatch.setattr(mergify, "api_get", lambda _repo, suffix: (
        {"batches": [], "mode": "serial"} if suffix.startswith("/merge-queue/status") else
        {"configuration": [{"name": "main", "config": {"allow_inplace_checks": True}}]}
    ))
    def github_config(text, *, default="main", changed=False):
        reads = 0
        def get(path):
            nonlocal reads
            if path == "repos/acme/widgets":
                return {"default_branch": default}
            assert path == "repos/acme/widgets/contents/.mergify.yml?ref=refs%2Fheads%2Fmain"
            reads += 1
            return {"type": "file", "encoding": "base64", "sha": str(reads if changed else 1)*40,
                    "content": base64.b64encode(text.encode()).decode()}
        monkeypatch.setattr(mergify.github, "api_get", get)
    return github_config


SAFE_CONFIG = "merge_queue: {mode: serial, max_parallel_checks: 3}\nqueue_rules: [{name: main}]\n"


def test_configured_accepts_documented_speculation_despite_raw_inplace_default(queue_api):
    queue_api(SAFE_CONFIG)
    assert mergify.configured("acme/widgets", "main", "main")


@pytest.mark.parametrize("text", [
    SAFE_CONFIG.replace("checks: 3", f"checks: {value}")
    for value in ["1", "0", "-1", "true", '"3"', "null"]
] + [
    SAFE_CONFIG.replace("mode: serial", "mode: parallel"),
    SAFE_CONFIG.replace("name: main", "name: other"),
    SAFE_CONFIG + "extends: [other/repo]\n",
    SAFE_CONFIG + "merge_queue: {max_parallel_checks: 1}\n",
    "[]", "!!python/object:object {}", "merge_queue: [",
])
def test_configured_rejects_unsafe_or_ambiguous_canonical_config(queue_api, text):
    queue_api(text)
    assert not mergify.configured("acme/widgets", "main", "main")


@pytest.mark.parametrize("options", [{"default": "other"}, {"changed": True}])
def test_configured_rejects_wrong_default_branch_or_config_change(queue_api, options):
    queue_api(SAFE_CONFIG, **options)
    assert not mergify.configured("acme/widgets", "main", "main")


def test_configured_still_requires_vendor_queue_identity(queue_api, monkeypatch):
    queue_api(SAFE_CONFIG)
    monkeypatch.setattr(mergify, "api_get", lambda *_: {"batches": [], "mode": "serial", "configuration": []})
    assert not mergify.configured("acme/widgets", "main", "main")


@pytest.mark.parametrize("field,value", [("encoding", "none"), ("type", "symlink"),
                                        ("sha", None), ("sha", "z"*40), ("content", None), ("content", "!")])
def test_configured_rejects_malformed_github_file(queue_api, monkeypatch, field, value):
    queue_api(SAFE_CONFIG)
    original = mergify.github.api_get
    def malformed(path):
        data = original(path)
        if "/contents/" in path:
            data[field] = value
        return data
    monkeypatch.setattr(mergify.github, "api_get", malformed)
    assert not mergify.configured("acme/widgets", "main", "main")


def test_legacy_false_does_not_license_single_check_source_updates(queue_api, monkeypatch):
    queue_api(SAFE_CONFIG.replace("checks: 3", "checks: 1"))
    monkeypatch.setattr(mergify, "api_get", lambda _repo, suffix: (
        {"batches": [], "mode": "serial"} if suffix.startswith("/merge-queue/status") else
        {"configuration": [{"name": "main", "config": {"allow_inplace_checks": False}}]}
    ))
    assert not mergify.configured("acme/widgets", "main", "main")


def test_markers_only_accept_exact_lines_from_current_login(monkeypatch):
    head, marker = "a" * 40, f"<!-- loopzero:mergify-confirmed head={'a' * 40} queue=main -->"
    rows = [("trusted", "@mergifyio queue main"), ("attacker", marker),  # early queue: no request
            ("trusted", f"prefix {marker}"), ("trusted", marker), ("attacker", "@mergifyio requeue")]
    comments = [{"body": body, "user": {"login": login}} for login, body in rows]
    monkeypatch.setattr(mergify.github, "login", lambda: "trusted")
    monkeypatch.setattr(mergify.github, "_paged", lambda *_: comments)
    assert mergify.markers("acme/widgets", 7, head, "main") == (False, True)
    comments.append({"body": "@mergifyio requeue", "user": {"login": "trusted"}})
    assert mergify.markers("acme/widgets", 7, head, "main") == (True, False)  # awaits admission


@pytest.mark.parametrize(("writer", "body"), [
    (mergify.request, "@mergifyio queue main\n\n<!-- loopzero:mergify-request head=abc queue=main -->"),
    (mergify.mark_confirmed, "<!-- loopzero:mergify-confirmed head=abc queue=main -->"),
])
def test_comment_writes_use_github_rest_api(monkeypatch, writer, body):
    calls = []

    def run(argv, **_kwargs):
        with open(argv[-1], encoding="utf-8") as handle:
            payload = json.load(handle)
        calls.append((argv[1:-1], payload))
        return type("Done", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mergify.github, "run", run)
    writer("acme/widgets", 7, "abc", "main")

    assert calls == [([
        "api", "repos/acme/widgets/issues/7/comments", "--method", "POST", "--input",
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
def test_malformed_membership_never_confirms(monkeypatch, field, value):
    payload = {"number": 7, "position": 1, "queued_at": "now", "queue_rule_name": "main"}
    payload[field] = value
    monkeypatch.setattr(mergify, "api_get", lambda *_: payload)
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


@pytest.mark.parametrize("failure", ["http503", "dropped_read", "truncated"])
def test_api_get_retries_transient_reads(monkeypatch, failure):
    calls = []

    class Body(io.BytesIO):
        def read(self, *args):
            if len(calls) == 1 and failure != "http503":  # body read fails once
                raise {"dropped_read": ConnectionResetError(), "truncated": mergify.http.client.IncompleteRead(b"{")}[failure]
            return super().read(*args)

    def fake_open(request):
        calls.append(request.get_method())
        if failure == "http503" and len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "x", {}, None)
        return Body(b"{}")

    monkeypatch.setattr(mergify, "_open", fake_open)
    monkeypatch.setattr(mergify, "_retry_sleep", lambda _: None)
    monkeypatch.setattr(mergify, "_credential", lambda: "k")
    assert mergify.api_get("acme/widgets", "/merge-queue/status") == {}
    assert calls == ["GET", "GET"]
