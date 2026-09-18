"""GitHub operations through the `gh` CLI: pull requests, reviews, checks, merge."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loopzero._proc import ToolMissing, run
from loopzero.types import Finding, ReviewResult

GH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")
BLOCKING = frozenset({"critical", "important"})
MARKER_RE = re.compile(
    r"<!--\s*loopzero:finding\s+(?:v=(?P<version>1)\s+)?"
    r"severity=(?P<severity>\w+)(?:\s+head=(?P<head>[0-9a-fA-F]+))?"
    r"(?:\s+id=(?P<id>[0-9a-fA-F]{8}))?\s*-->"
)
PR_FIELDS = "number,url,headRefOid,baseRefName,isDraft,state,mergeable,author"
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
          isResolved isOutdated path line
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


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]


def _gh(*args: str, cwd: Path | None = None, timeout: float = 120) -> str:
    argv = ("gh", *args)
    try:
        done = run(list(argv), cwd=cwd or Path.cwd(), env_allowlist=GH_ENV, timeout=timeout)
    except ToolMissing as exc:
        raise GhMissing(argv, f"{exc}\nInstall GitHub CLI, then {GH_AUTH_REMEDY}") from exc
    if done.exit_code != 0:
        tail = "\n".join(part for part in (done.stderr, done.stdout) if part)
        if "gh auth login" in tail or "not logged in" in tail.lower():
            raise GhAuth(argv, f"{tail.rstrip()}\n{GH_AUTH_REMEDY}")
        raise GhError(argv, tail)
    return done.stdout


def _gh_json(*args: str) -> object:
    out = _gh(*args)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as exc:
        raise GhError(("gh", *args), f"invalid JSON from gh: {out[-600:]}") from exc


def _paged(endpoint: str) -> list:
    """Collect a list endpoint page by page until a short page."""
    items: list = []
    page = 1
    while True:
        chunk = _gh_json("api", endpoint, "--method", "GET", "-F", f"per_page={PAGE}",
                         "-F", f"page={page}") or []
        if isinstance(chunk, dict):  # check-runs wraps the list
            chunk = chunk.get("check_runs") or []
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


def login() -> str:
    """Login of the user the `gh` token belongs to."""
    data = api_get("user")
    name = data.get("login") if isinstance(data, dict) else None
    if not name:
        raise GhError(("gh", "api", "user"), f"no login in response: {data!r}")
    return str(name)


def _not_found(number: int, exc: Exception) -> GhError:
    return GhError(("gh", "api"), f"PR #{number} not found or not accessible ({exc!r})")


def _api(endpoint: str, payload: dict | None = None, method: str = "POST") -> object:
    if payload is None:
        return api_get(endpoint)
    path = _with_file(json.dumps(payload))
    try:
        return _gh_json("api", endpoint, "--method", method, "--input", str(path))
    finally:
        path.unlink(missing_ok=True)


def _pr_from_json(data: dict) -> PR:
    try:
        return PR(
            number=int(data["number"]),
            url=data["url"],
            head_sha=data["headRefOid"],
            base_ref=data["baseRefName"],
            is_draft=bool(data["isDraft"]),
            state=data["state"],
            mergeable=data.get("mergeable") or "UNKNOWN",
            author=str((data.get("author") or {}).get("login") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        number = data.get("number", "?") if isinstance(data, dict) else "?"
        raise _not_found(number, exc) from exc


def pr_for_branch(repo: str, branch: str) -> PR | None:
    """Return the open PR for `branch`, else the most recent one, else None."""
    data = _gh_json(
        "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
        "--limit", "10", "--json", PR_FIELDS,
    )
    prs = [_pr_from_json(p) for p in (data or [])]
    if not prs:
        return None
    return next((p for p in prs if p.state == "OPEN"), prs[0])


def pr_view(repo: str, number: int) -> PR:
    data = _gh_json("pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS)
    if not isinstance(data, dict):
        raise _not_found(number, ValueError(repr(data)))
    return _pr_from_json(data)


def create_draft_pr(repo: str, branch: str, base: str, title: str, body: str) -> PR:
    path = _with_file(body)
    try:
        _gh(
            "pr", "create", "--repo", repo, "--head", branch, "--base", base,
            "--title", title, "--body-file", str(path), "--draft",
        )
    finally:
        path.unlink(missing_ok=True)
    pr = pr_for_branch(repo, branch)
    if pr is None:
        raise GhError(("gh", "pr", "create"), f"PR for {branch} not visible after create")
    return pr


def update_body(repo: str, number: int, body: str) -> None:
    _api(f"repos/{repo}/pulls/{number}", {"body": body}, method="PATCH")


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
    lines = [prefix.rstrip("\n")] if prefix else []
    lines += [
        (
            f"loopzero {result.kind} review by {result.family} on `{result.head}`: "
            f"**{result.verdict}** ({summary})"
        ),
    ]
    if unplaced:
        lines.append("\nFindings without a file location:\n")
        lines += [f"- **{f.severity}**: {f.title} — {f.body}".rstrip(" —") for f in unplaced]
    lines.append(
        f"\n<details><summary>Raw reviewer output</summary>\n\n```\n{result.raw}\n```\n</details>"
    )
    return "\n".join(lines)


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
) -> None:
    """Post `result` as a PR review on `head_sha`, one inline comment per finding.

    `body_prefix`, when given, becomes the first line of the review body. When the
    token user authored the PR (`pr` is fetched if not given) the review is posted
    as a COMMENT, since GitHub rejects self-approval and self-request-changes.

    Findings whose location is missing or outside the diff are anchored on the first
    changed file with a hunk, so every blocking finding creates a review thread.
    """
    author = (pr or pr_view(repo, number)).author
    if author and author == login():
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
        _api(endpoint, payload)
    except GhError as exc:
        if event == "COMMENT" or not _SELF_REVIEW.search(exc.tail):
            raise
        _api(endpoint, {**payload, "event": "COMMENT"})


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


def open_blocking_findings(
    repo: str, number: int, current_head: str | None = None
) -> list[Finding]:
    """Findings from unresolved review threads whose first comment carries a blocking marker.

    Only explicit critical and important markers block. When `current_head` is supplied, an
    outdated thread is ignored unless its marker names that head.
    """
    owner, name = repo.split("/", 1)
    after: str | None = None
    found: list[Finding] = []
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
            for node in nodes:
                if node.get("isResolved"):
                    continue
                finding = _parse_thread(node, current_head)
                if finding:
                    found.append(finding)
            if not page["hasNextPage"]:
                return found
            after = page["endCursor"]
        except (KeyError, TypeError) as exc:
            raise _not_found(number, exc) from exc


def check_runs(repo: str, head_sha: str) -> dict[str, str]:
    """Map signal name to state, using only the latest check run of each name."""
    signals: dict[str, list[str]] = {}
    latest_runs: dict[str, dict] = {}
    for cr in _paged(f"repos/{repo}/commits/{head_sha}/check-runs"):
        previous = latest_runs.get(cr["name"])
        if previous is None or _check_run_order(cr) > _check_run_order(previous):
            latest_runs[cr["name"]] = cr
    for name, cr in latest_runs.items():
        signals[name] = [cr.get("conclusion") or cr.get("status") or "unknown"]
    latest_status: dict[str, str] = {}
    for status in _paged(f"repos/{repo}/commits/{head_sha}/statuses"):
        latest_status.setdefault(status["context"], status.get("state") or "unknown")
    for name, state in latest_status.items():
        signals.setdefault(name, []).append(state)
    return {name: _aggregate_signal(states) for name, states in signals.items()}


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
    repo: str, pr: PR, required_ci: tuple[str, ...], reviewed_head: str | None
) -> Readiness:
    """Decide whether `pr` may be marked ready / merged. Never raises on policy failures."""
    reasons: list[str] = []
    if pr.state != "OPEN":
        reasons.append(f"PR #{pr.number} is {pr.state}, not open")
    if pr.mergeable == "CONFLICTING":
        reasons.append(f"PR #{pr.number} has merge conflicts with {pr.base_ref}")
    if not reviewed_head:
        reasons.append("no review recorded for the current head")
    elif pr.head_sha != reviewed_head:
        reasons.append(f"head {pr.head_sha[:12]} differs from reviewed {reviewed_head[:12]}")
    for f in open_blocking_findings(repo, pr.number, pr.head_sha):
        where = f"{f.path}:{f.line}" if f.path else "(no location)"
        reasons.append(f"open {f.severity} finding at {where}: {f.title}")
    checks = check_runs(repo, pr.head_sha) if required_ci else {}
    for name in required_ci:
        conclusion = checks.get(name)
        if conclusion is None:
            reasons.append(f"required check '{name}' missing on {pr.head_sha[:12]}")
        elif conclusion != "success":
            reasons.append(f"required check '{name}' is {conclusion}")
    return Readiness(ready=not reasons, reasons=tuple(reasons))


def mark_ready(repo: str, number: int) -> None:
    _gh("pr", "ready", str(number), "--repo", repo)


def merge(repo: str, number: int, strategy: str, head_sha: str) -> str:
    """Merge PR `number` at exactly `head_sha`, delete the remote branch, return the merge SHA."""
    if strategy not in ("squash", "merge", "rebase"):
        raise MergeFailed(("gh", "pr", "merge"), f"unknown merge strategy: {strategy}")
    argv = (
        "pr", "merge", str(number), "--repo", repo, f"--{strategy}",
        "--match-head-commit", head_sha,
    )
    try:
        _gh(*argv, timeout=300)
    except GhError as exc:
        raise MergeFailed(exc.command, exc.tail) from exc
    view = _gh_json(
        "pr", "view", str(number), "--repo", repo, "--json", "mergeCommit,state,headRefName"
    ) or {}
    sha = (view.get("mergeCommit") or {}).get("oid")
    if view.get("state") != "MERGED" or not sha:
        raise MergeFailed(("gh", *argv), f"PR #{number} not verified merged: {view!r}")
    branch = view.get("headRefName")
    if branch:
        try:
            _gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}")
        except GhError as exc:
            absent = re.search(r"\b(?:404|422)\b", exc.tail) and re.search(
                r"reference does not exist", exc.tail, re.IGNORECASE
            )
            if not absent:
                raise
    return sha
