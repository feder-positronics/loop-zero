"""GitHub operations through the `gh` CLI: pull requests, reviews, checks, merge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from loopzero._proc import ProcTimeout, ToolMissing, run
from loopzero.types import Finding, ReviewResult

GH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")
APP_TOKEN_HELPER = Path.home() / ".config/loopzero/app-token"
_app_token_cache = ("", 0.0)
BLOCKING = frozenset({"critical", "important"})
MARKER_RE = re.compile(
    r"<!--\s*loopzero:finding\s+(?:v=(?P<version>1)\s+)?"
    r"severity=(?P<severity>\w+)(?:\s+head=(?P<head>[0-9a-fA-F]+))?"
    r"(?:\s+id=(?P<id>[0-9a-fA-F]{8}))?\s*-->"
)
PAGE = 100
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
          comments(first: 1) { nodes { body } }
        }
      }
    }
  }
}
"""


class GhError(Exception):
    def __init__(self, command: tuple[str, ...], tail: str) -> None:
        self.command = command
        self.tail = tail
        super().__init__(f"{' '.join(command)} failed: {tail.strip()[-600:]}")


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


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]
    waitable_failures: tuple[str, ...] = ()


TRANSIENT_RE = re.compile(r"HTTP 5\d\d|timed out|connection reset|no server is currently",
                          re.IGNORECASE)
RETRY_DELAYS = (5.0, 20.0, 60.0)
_retry_sleep = time.sleep


def _read_only(args: tuple[str, ...]) -> bool:
    """Only reads are retried: a repeated write could post or change state twice."""
    if args[:2] == ("api", "graphql"):
        return not any("mutation" in arg for arg in args)
    if args[:1] == ("api",):
        methods = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg in ("-X", "--method")]
        if methods:
            return all(method.upper() == "GET" for method in methods)
        return not {"-f", "-F", "--field", "--raw-field", "--input"} & set(args)
    return args[:2] in {("pr", "view"), ("pr", "list"), ("pr", "checks")}


def app_token() -> str:
    global _app_token_cache
    token, expires = _app_token_cache
    if token and time.monotonic() < expires:
        return token
    if not APP_TOKEN_HELPER.is_file() or not os.access(APP_TOKEN_HELPER, os.X_OK):
        return ""
    try:
        result = run([str(APP_TOKEN_HELPER)], cwd=Path.cwd(), env_allowlist=GH_ENV, timeout=10)
    except (ToolMissing, ProcTimeout, OSError):
        return ""
    if result.exit_code or not result.stdout.strip():
        return ""
    token = result.stdout.strip()
    _app_token_cache = (token, time.monotonic() + 3000)
    return token


def _gh(*args: str, cwd: Path | None = None, timeout: float = 120,
        token: str | None = None) -> str:
    argv = ("gh", *args)
    selected = token if token is not None else (app_token() if _read_only(args) else "")
    for delay in (*RETRY_DELAYS, None):
        try:
            done = run(list(argv), cwd=cwd or Path.cwd(), env_allowlist=GH_ENV,
                       extra_env={"GH_TOKEN": selected} if selected else None, timeout=timeout)
        except ToolMissing as exc:
            raise GhMissing(argv, f"{exc}\nInstall GitHub CLI, then {GH_AUTH_REMEDY}") from exc
        except ProcTimeout:
            if delay is None or not _read_only(args):
                raise
            _retry_sleep(delay)
            continue
        tail = "\n".join(part for part in (done.stderr, done.stdout) if part)
        if done.exit_code and re.search(r"rate limit|HTTP 429", tail, re.IGNORECASE):
            reset = _graphql_reset(args, tail, cwd or Path.cwd(), selected)
            raise GhError(argv, tail + reset + "\nRate limited: retry after GitHub's reset or Retry-After; "
                          "No automatic retry.")
        if done.exit_code == 0 or delay is None or not _read_only(args) or not TRANSIENT_RE.search(tail):
            break
        _retry_sleep(delay)
    if done.exit_code != 0:
        if "gh auth login" in tail or "not logged in" in tail.lower():
            raise GhAuth(argv, f"{tail.rstrip()}\n{GH_AUTH_REMEDY}")
        raise GhError(argv, tail)
    return done.stdout


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
                     timeout=10)
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
    except (ToolMissing, ProcTimeout, KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    return ""


def _gh_json(*args: str, token: str | None = None) -> object:
    out = _gh(*args, token=token)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as exc:
        raise GhError(("gh", *args), f"invalid JSON from gh: {out[-600:]}") from exc


def _paged(endpoint: str, key: str = "check_runs") -> list:
    """Collect a list endpoint page by page until a short page."""
    items: list = []
    page = 1
    while True:
        chunk = _gh_json("api", endpoint, "--method", "GET", "-F", f"per_page={PAGE}",
                         "-F", f"page={page}") or []
        if isinstance(chunk, dict):
            chunk = chunk.get(key) or []
        items += chunk
        if len(chunk) < PAGE:
            return items
        page += 1


def _with_file(content: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".loopzero", text=True)
    with open(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return Path(name)


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


def _api(endpoint: str, payload: dict | None = None, method: str = "POST",
         token: str | None = None) -> object:
    if payload is None:
        return api_get(endpoint)
    path = _with_file(json.dumps(payload))
    try:
        return _gh_json("api", endpoint, "--method", method, "--input", str(path), token=token)
    finally:
        path.unlink(missing_ok=True)


def _pr_from_rest(data: object) -> PR:
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
                or mergeable is not None and type(mergeable) is not bool
                or merge_state is not None and not isinstance(merge_state, str)
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
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        number = data.get("number", "?") if isinstance(data, dict) else "?"
        raise _not_found(number, exc) from exc


def _pr_detail(repo: str, number: int) -> tuple[PR, dict]:
    data = api_get(f"repos/{repo}/pulls/{number}")
    pr = _pr_from_rest(data)
    if pr.number != number:
        raise GhError(("gh", "api", f"repos/{repo}/pulls/{number}"),
                      f"PR detail number {pr.number} differs from requested {number}")
    return pr, data


def pr_for_branch(repo: str, branch: str) -> PR | None:
    """Return the open PR for `branch`, else the most recent one, else None."""
    owner = repo.split("/", 1)[0]
    head = quote(f"{owner}:{branch}", safe="")
    for state in ("open", "closed"):
        endpoint = (f"repos/{repo}/pulls?state={state}&head={head}"
                    "&sort=created&direction=desc&per_page=1")
        rows = api_get(endpoint)
        if not isinstance(rows, list):
            raise GhError(("gh", "api", endpoint), "invalid PR list response")
        if rows:
            number = rows[0].get("number") if isinstance(rows[0], dict) else None
            if type(number) is not int or number < 1:
                raise GhError(("gh", "api", endpoint), "invalid PR number in list")
            pr, detail = _pr_detail(repo, number)
            source = detail.get("head") or {}
            source_repo = source.get("repo") or {}
            source_name = source_repo.get("full_name") if isinstance(source_repo, dict) else None
            if (source.get("ref") != branch or
                    not isinstance(source_name, str) or source_name.casefold() != repo.casefold()
                    or (pr.state == "OPEN") != (state == "open")):
                raise GhError(("gh", "api", endpoint), "PR changed during branch lookup")
            return pr
    return None


def pr_view(repo: str, number: int) -> PR:
    return _pr_detail(repo, number)[0]


def create_draft_pr(repo: str, branch: str, base: str, title: str, body: str) -> PR:
    detail = _api(
        f"repos/{repo}/pulls", {"head": branch, "base": base, "title": title,
                               "body": body, "draft": True})
    pr = _pr_from_rest(detail)
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


def _review_body(result: ReviewResult, unplaced: list[Finding], prefix: str = "") -> str:
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in
              ("critical", "important", "suggestion")}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    duration = "unknown" if result.duration_s is None else f"{result.duration_s:.1f}s"
    effort = f" (effort {result.effort})" if result.effort else ""
    lines = [prefix.rstrip("\n")] if prefix else []
    lines += [
        (
            f"loopzero {result.kind} review by {result.family} on `{result.head}`, "
            f"model {result.model or 'unknown'}{effort}, {duration}: "
            f"**{result.verdict}** ({summary})"
        ),
    ]
    if result.chunk_count > 1:
        lines.append(f"\nReviewed in {result.chunk_count} chunks with the same model family.")
    if unplaced:
        lines.append("\nFindings without a file location:\n")
        lines += [f"- **{f.severity}**: {f.title} — {f.body}".rstrip(" —") for f in unplaced]
    raw = result.raw
    encoded = raw.encode("utf-8")
    if len(encoded) > 30_000:
        raw = encoded[-30_000:].decode("utf-8", errors="ignore")
        raw = (
            "[Transcript truncated; showing the last at most 30,000 UTF-8 bytes. "
            "Full output remains in the saved local review artifact.]\n" + raw
        )
    return "\n".join(lines) + (
        f"\n\n<details><summary>Raw reviewer output</summary>\n\n```\n{raw}\n```\n</details>"
    )


def _right_lines(patch: str) -> tuple[set[int], int | None]:
    """Right-side (new file) line numbers present in `patch`, and the first added line."""
    lines: set[int] = set()
    first_added: int | None = None
    current = None
    for raw in patch.splitlines():
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


def _diff_map(repo: str, number: int) -> tuple[dict[str, set[int]], dict | None]:
    """Commentable lines per path, plus the anchor (first file with a hunk, first added line)."""
    commentable: dict[str, set[int]] = {}
    anchor: dict | None = None
    for entry in _paged(f"repos/{repo}/pulls/{number}/files"):
        lines, first_added = _right_lines(entry.get("patch") or "")
        if not lines:
            continue
        commentable[entry["filename"]] = lines
        if anchor is None:
            line = first_added if first_added is not None else min(lines)
            anchor = {"path": entry["filename"], "line": line, "side": "RIGHT"}
    return commentable, anchor


def post_review(
    repo: str,
    number: int,
    head_sha: str,
    result: ReviewResult,
    body_prefix: str = "",
    pr: PR | None = None,
    token: str | None = None,
) -> None:
    """Post `result` as a PR review on `head_sha`, one inline comment per finding.

    `body_prefix`, when given, becomes the first line of the review body. When the
    token user authored the PR (`pr` is fetched if not given) the review is posted
    as a COMMENT, since GitHub rejects self-approval and self-request-changes.

    Findings whose location is missing or outside the diff are anchored on the first
    changed file with a hunk, so every blocking finding creates a review thread.
    """
    author = (pr or pr_view(repo, number)).author
    token = app_token() if token is None else token  # "" means the operator identity
    if token or author and author == login(token=token):
        event = "COMMENT"
    else:
        event = "APPROVE" if result.verdict == "approve" else "REQUEST_CHANGES"
    commentable, anchor = _diff_map(repo, number)
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
    payload = {
        "commit_id": head_sha,
        "event": event,
        "body": _review_body(result, loose, body_prefix),
        "comments": comments,
    }
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


def _parse_thread(node: dict, current_head: str | None) -> Finding | None:
    comments = node.get("comments", {}).get("nodes") or []
    if not comments:
        return None
    first = comments[0]
    body = first.get("body") or ""
    match = MARKER_RE.search(body)
    if not match or match.group("severity") not in BLOCKING:
        return None
    version = match.group("version")
    marker_head = match.group("head")
    marker_id = match.group("id")
    if version and (not marker_head or not marker_id):
        return None
    if not version and marker_id:
        return None
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


def open_findings(repo: str, number: int) -> list[tuple[str, str, Finding]]:
    """(marker id, thread id, finding) for every unresolved blocking thread, any head."""
    return [
        (MARKER_RE.search(node["comments"]["nodes"][0]["body"]).group("id") or node["id"], node["id"], f)
        for node in _unresolved_threads(repo, number)
        if (f := _parse_thread(node, None))
    ]


def resolve_finding(repo: str, number: int, finding_id: str, reply: str) -> Finding:
    """Reply in the thread of open finding `finding_id` and resolve it."""
    for marker_id, thread_id, finding in open_findings(repo, number):
        if marker_id == finding_id:
            # Two calls on purpose: fields of one mutation are not transactional, and a
            # thread must never be resolved unless its reply was confirmed.
            posted = _gh_json("api", "graphql", "-f", f"query={_REPLY_MUTATION}",
                              "-F", f"thread={thread_id}", "-f", f"body={reply}")
            try:
                posted["data"]["addPullRequestReviewThreadReply"]["comment"]["id"]
            except (KeyError, TypeError) as exc:
                raise GhError(("resolve", finding_id), "reply was not posted; not resolving") from exc
            _gh("api", "graphql", "-f", f"query={_RESOLVE_MUTATION}", "-F", f"thread={thread_id}")
            return finding
    raise GhError(("resolve", finding_id), f"no open blocking finding {finding_id} on #{number}")


def open_blocking_findings(
    repo: str, number: int, current_head: str | None = None
) -> list[Finding]:
    """Findings from unresolved review threads whose first comment carries a blocking marker.

    Only explicit critical and important markers block. When `current_head` is supplied, an
    outdated thread is ignored unless its marker names that head.
    """
    return [
        f for node in _unresolved_threads(repo, number) if (f := _parse_thread(node, current_head))
    ]


def _unresolved_threads(repo: str, number: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    after: str | None = None
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
            found += [node for node in nodes if not node.get("isResolved")]
            if not page["hasNextPage"]:
                return found
            after = page["endCursor"]
        except (KeyError, TypeError) as exc:
            raise _not_found(number, exc) from exc


def check_runs(repo: str, head_sha: str) -> dict[str, str]:
    """Map signal name to state, using only the latest check run of each name."""
    return _check_signals(repo, head_sha)[0]


def _check_signals(repo: str, head_sha: str, review_at: str | None = None, review_grace: bool = False,
                   required_ci: tuple[str, ...] = ()
                   ) -> tuple[dict[str, str], tuple[str, ...]]:
    signals: dict[str, list[tuple[str, str | None]]] = {}
    latest_runs: dict[str, dict] = {}
    for cr in _paged(f"repos/{repo}/commits/{head_sha}/check-runs"):
        previous = latest_runs.get(cr["name"])
        if previous is None or _check_run_order(cr) > _check_run_order(previous):
            latest_runs[cr["name"]] = cr
    for name, cr in latest_runs.items():
        signals[name] = [(cr.get("conclusion") or cr.get("status") or "unknown",
                          cr.get("completed_at") or cr.get("started_at"))]
    latest_status: dict[str, dict] = {}
    for status in _paged(f"repos/{repo}/commits/{head_sha}/statuses"):
        latest_status.setdefault(status["context"], status)
    for name, status in latest_status.items():
        signals.setdefault(name, []).append((status.get("state") or "unknown",
                                               status.get("created_at")))
    states = {name: _aggregate_signal([state for state, _ in values])
              for name, values in signals.items()}
    pending = {"pending", "queued", "in_progress", "requested", "waiting", "expected"}
    stale = tuple(name for name, values in signals.items() if review_at
                  and states[name] not in pending and states[name] != "success"
                  and all(at and at < review_at for state, at in values
                          if state != "success" and state not in pending)
                  and (review_grace or any(at and at >= review_at
                                           for state, at in values if state in pending)))
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
        if _has_active_replacement(cr, workflows, head_sha):
            superseded.append(name)
    return states, (*stale, *superseded)


def _has_active_replacement(check: dict, workflows: list[dict], head_sha: str) -> bool:
    """A producer can be superseded only by its own workflow on the same head."""
    suite_id = check["check_suite"]["id"]
    producer = next((workflow for workflow in workflows if workflow.get("check_suite_id") == suite_id
                     and workflow.get("head_sha") == head_sha), None)
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


def readiness(
    repo: str,
    pr: PR,
    base_branch: str,
    required_ci: tuple[str, ...],
    reviewed_head: str | None,
    *, allow_behind: bool = False,
    review_at: str | None = None,
    review_grace: bool = False,
    workflow_wait: bool = False,
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
        reasons.append(f"PR #{pr.number} is behind {pr.base_ref}; rebase and rerun checks")
    if not reviewed_head:
        reasons.append("no review recorded for the current head")
    elif pr.head_sha != reviewed_head:
        reasons.append(f"head {pr.head_sha[:12]} differs from reviewed {reviewed_head[:12]}")
    for f in open_blocking_findings(repo, pr.number, pr.head_sha):
        where = f"{f.path}:{f.line}" if f.path else "(no location)"
        reasons.append(f"open {f.severity} finding at {where}: {f.title}")
    checks, stale = (_check_signals(repo, pr.head_sha, review_at, review_grace,
                                   required_ci if workflow_wait else ())
                     if required_ci else ({}, ()))
    for name in required_ci:
        conclusion = checks.get(name)
        if conclusion is None:
            reasons.append(f"required check '{name}' missing on {pr.head_sha[:12]}")
        elif conclusion != "success":
            reasons.append(f"required check '{name}' is {conclusion}")
    return Readiness(ready=not reasons, reasons=tuple(reasons),
                     waitable_failures=tuple(f"required check '{name}' is {checks[name]}"
                                             for name in required_ci if name in stale))


def mark_ready(repo: str, number: int) -> None:
    _gh("pr", "ready", str(number), "--repo", repo)


_QUEUE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){isInMergeQueue autoMergeRequest{enabledAt}}}}"
)


def merged_sha(repo: str, number: int, *, expected_head: str | None = None) -> str | None:
    """The merge commit of PR `number` once GitHub reports it MERGED, else None."""
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
        return None  # An open PR's merge_commit_sha is only a test merge.
    raise GhError(argv, "PR closed without a verified merge or invalid merge evidence")


def merge_pending(repo: str, number: int) -> bool:
    """True when GitHub holds the PR for merging: queued, or accepted as an auto-merge
    request that it moves into the queue shortly after (observed ~1 minute, #180)."""
    owner, name = repo.split("/", 1)
    data = _gh_json(
        "api", "graphql", "-f", f"query={_QUEUE_QUERY}", "-F", f"owner={owner}",
        "-F", f"name={name}", "-F", f"number={number}",
    )
    try:
        pr = data["data"]["repository"]["pullRequest"]
        return bool(pr["isInMergeQueue"] or pr.get("autoMergeRequest"))
    except (KeyError, TypeError):
        return False


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
    if merge_pending(repo, number):
        return None
    # The queue may have landed the PR between the two reads; look once more.
    sha = merged_sha(repo, number, expected_head=head_sha)
    if sha:
        return sha
    raise MergeFailed(("gh", *argv), f"PR #{number} neither merged nor queued")


def delete_remote_branch(repo: str, branch: str) -> None:
    """Delete `branch` from GitHub, accepting a ref that is already absent."""
    try:
        _gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}")
    except GhError as exc:
        absent = re.search(r"\b(?:404|422)\b", exc.tail) and re.search(
            r"reference does not exist", exc.tail, re.IGNORECASE
        )
        if not absent:
            raise


def commit_status(repo: str, head: str, context: str, state: str, description: str) -> None:
    """Publish a SHA-scoped status using the caller's narrowly scoped credential."""
    _api(f"repos/{repo}/statuses/{head}",
         {"state": state, "context": context, "description": description})
