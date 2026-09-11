"""Publication admission through the shared paginated review-thread reader."""

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ._publish_gate import GateError
from ._publish_paths import PublicationError


class JsonRunner(Protocol):
    def run_json(self, args: list[str]) -> object: ...


class _ReviewThreadRunner:
    """Adapt publication's JSON transport to the shared merge-thread reader."""

    def __init__(self, runner: JsonRunner) -> None:
        self.runner = runner

    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(self.runner.run_json(list(args)))
        )


_REVIEW_THREADS_QUERY = """
query($url: URI!, $cursor: String) {
  resource(url: $url) {
    ... on PullRequest {
      reviewThreads(first: 100, after: $cursor) {
        nodes { id isResolved isOutdated path comments(last: 1) { nodes { url } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


def load_actionable_review_threads(
    runner: _ReviewThreadRunner, pr_url: str
) -> tuple[dict[str, str], ...]:
    if not pr_url:
        raise GateError("PR review-thread state is unavailable: PR URL is missing.")
    actionable: list[dict[str, str]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        command = [
            "gh", "api", "graphql", "-f", f"query={_REVIEW_THREADS_QUERY}",
            "-f", f"url={pr_url}",
        ]
        if cursor is not None:
            command.extend(["-f", f"cursor={cursor}"])
        try:
            payload = json.loads(runner.run(command).stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise GateError("Command did not return valid JSON: gh api graphql") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise GateError(
                "PR review-thread state is unavailable: GitHub GraphQL returned "
                "errors or an invalid response."
            )
        data = payload.get("data")
        resource = data.get("resource") if isinstance(data, dict) else None
        threads = resource.get("reviewThreads") if isinstance(resource, dict) else None
        nodes = threads.get("nodes") if isinstance(threads, dict) else None
        page_info = threads.get("pageInfo") if isinstance(threads, dict) else None
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GateError(
                "PR review-thread state is unavailable: GitHub returned an "
                "incomplete thread connection."
            )
        for node in nodes:
            if not isinstance(node, dict):
                raise GateError("PR review-thread state is unavailable: GitHub returned an invalid thread.")
            resolved = node.get("isResolved")
            outdated = node.get("isOutdated")
            thread_id = node.get("id")
            if not isinstance(resolved, bool) or not isinstance(outdated, bool) or not isinstance(thread_id, str):
                raise GateError("PR review-thread state is unavailable: GitHub returned an invalid thread.")
            if resolved:
                continue
            comments = node.get("comments")
            comment_nodes = comments.get("nodes") if isinstance(comments, dict) else None
            url = ""
            if isinstance(comment_nodes, list) and comment_nodes and isinstance(comment_nodes[-1], dict):
                raw_url = comment_nodes[-1].get("url")
                url = raw_url if isinstance(raw_url, str) else ""
            raw_path = node.get("path")
            actionable.append({
                "id": thread_id,
                "path": raw_path if isinstance(raw_path, str) else "<unknown>",
                "url": url,
            })
        has_next = page_info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise GateError("PR review-thread state is unavailable: GitHub pagination is invalid.")
        if not has_next:
            return tuple(actionable)
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise GateError("PR review-thread state is unavailable: GitHub pagination is invalid.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def require_resolved_review_threads(runner: JsonRunner, pr_url: str) -> None:
    """Refuse publication until every review thread is explicitly resolved."""
    try:
        threads = load_actionable_review_threads(_ReviewThreadRunner(runner), pr_url)
    except GateError as exc:
        raise PublicationError(str(exc)) from exc
    if threads:
        details = "\n".join(
            f"- {thread['id']}: {thread['path']} {thread['url']}" for thread in threads
        )
        raise PublicationError(
            "PR has unresolved review threads, including outdated threads; "
            "resolve them before publication:\n" + details
        )
