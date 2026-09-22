from __future__ import annotations

import io
import json
import urllib.error

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

    def open_(request):
        seen.append(request)
        return Response(b"")

    monkeypatch.setattr(mergify, "_open", open_)
    mergify.dequeue("acme/widgets", 7)
    assert seen[0].method == "POST"
    assert seen[0].full_url.endswith("/merge-queue/pull/7/dequeue")


def test_configured_requires_branch_status_and_named_rule(monkeypatch):
    responses = iter([
        {"batches": [], "waiting_pull_requests": [], "mode": "serial"},
        {"configuration": [{"name": "main", "config": {}}]},
    ])
    monkeypatch.setattr(mergify, "api_get", lambda *_: next(responses))
    assert mergify.configured("acme/widgets", "main", "main")


def test_markers_only_accept_exact_lines_from_current_login(monkeypatch):
    head = "a" * 40
    marker = f"<!-- loopzero:mergify-confirmed head={head} queue=main -->"
    monkeypatch.setattr(mergify.github, "login", lambda: "trusted")
    monkeypatch.setattr(mergify.github, "_paged", lambda *_: [
        {"body": marker, "user": {"login": "attacker"}},
        {"body": f"prefix {marker}", "user": {"login": "trusted"}},
        {"body": marker, "user": {"login": "trusted"}},
    ])
    assert mergify.markers("acme/widgets", 7, head, "main") == (False, True)


def test_credential_file_must_be_private(tmp_path, monkeypatch):
    monkeypatch.delenv("MERGIFY_API_KEY", raising=False)
    key = tmp_path / "api-key"
    key.write_text("secret\n")
    key.chmod(0o644)
    with pytest.raises(mergify.MergifyError, match="unsafe"):
        mergify._credential(key)
    key.chmod(0o600)
    assert mergify._credential(key) == "secret"
