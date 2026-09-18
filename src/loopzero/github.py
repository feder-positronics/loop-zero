"""GitHub operations through the `gh` CLI: pull requests, reviews, checks, merge."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loopzero._proc import ToolMissing, run
from loopzero.types import Finding, ReviewResult

GH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")
BLOCKING = frozenset({"critical", "important"})
MARKER_RE = re.compile(r"<!--\s*loopzero:finding\s+severity=(\w+)\s+head=([0-9a-fA-F]+)\s*-->")
PR_FIELDS = "number,url,headRefOid,baseRefName,isDraft,state,mergeable"
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


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]


def _gh(*args: str, cwd: Path | None = None, timeout: float = 120) -> str:
    argv = ("gh", *args)
    try:
        done = run(list(argv), cwd=cwd or Path.cwd(), env_allowlist=GH_ENV, timeout=timeout)
    except ToolMissing as exc:
        raise GhMissing(argv, str(exc)) from exc
    if done.exit_code != 0:
        tail = done.stderr or done.stdout
        if "gh auth login" in tail or "not logged in" in tail.lower():
            raise GhAuth(argv, tail)
        raise GhError(argv, tail)
    return done.stdout


def _gh_json(*args: str) -> object:
    out = _gh(*args)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as exc:
        raise GhError(("gh", *args), f"invalid JSON from gh: {out[-600:]}") from exc


def _with_file(content: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".loopzero", text=True)
    with open(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return Path(name)


def _api(endpoint: str, payload: dict | None = None, method: str = "POST") -> object:
    if payload is None:
        return _gh_json("api", endpoint)
    path = _with_file(json.dumps(payload))
    try:
        return _gh_json("api", endpoint, "--method", method, "--input", str(path))
    finally:
        path.unlink(missing_ok=True)


def _pr_from_json(data: dict) -> PR:
    return PR(
        number=int(data["number"]),
        url=data["url"],
        head_sha=data["headRefOid"],
        base_ref=data["baseRefName"],
        is_draft=bool(data["isDraft"]),
        state=data["state"],
        mergeable=data.get("mergeable") or "UNKNOWN",
    )


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
    path = _with_file(body)
    try:
        _gh("pr", "edit", str(number), "--repo", repo, "--body-file", str(path))
    finally:
        path.unlink(missing_ok=True)


def finding_marker(severity: str, head_sha: str) -> str:
    return f"<!-- loopzero:finding severity={severity} head={head_sha} -->"


def _comment_body(f: Finding, head_sha: str) -> str:
    text = f"**{f.severity}: {f.title}**\n\n{f.body}".rstrip()
    if f.severity in BLOCKING:
        return f"{finding_marker(f.severity, head_sha)}\n{text}"
    return text


def _review_body(result: ReviewResult, unplaced: list[Finding]) -> str:
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in
              ("critical", "important", "suggestion")}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    lines = [
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


def post_review(repo: str, number: int, head_sha: str, result: ReviewResult) -> None:
    """Post `result` as a PR review on `head_sha`, one inline comment per located finding."""
    placed = [f for f in result.findings if f.path and f.line]
    unplaced = [f for f in result.findings if not (f.path and f.line)]
    event = "APPROVE" if result.verdict == "approve" else "REQUEST_CHANGES"
    payload = {
        "commit_id": head_sha,
        "event": event,
        "body": _review_body(result, unplaced),
        "comments": [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": _comment_body(f, head_sha)}
            for f in placed
        ],
    }
    endpoint = f"repos/{repo}/pulls/{number}/reviews"
    try:
        _api(endpoint, payload)
    except GhError as exc:
        if not _SELF_REVIEW.search(exc.tail):
            raise
        _api(endpoint, {**payload, "event": "COMMENT"})


def _parse_thread(node: dict) -> Finding | None:
    comments = node.get("comments", {}).get("nodes") or []
    if not comments:
        return None
    body = comments[0].get("body") or ""
    first, _, rest = body.partition("\n")
    match = MARKER_RE.search(first)
    if not match or match.group(1) not in BLOCKING:
        return None
    title_line = rest.strip().splitlines()[0] if rest.strip() else ""
    title = re.sub(r"^\*\*\w+:\s*|\*\*$", "", title_line).strip()
    return Finding(
        severity=match.group(1), path=node.get("path"), line=node.get("line"),
        title=title, body=rest.strip(),
    )


def open_blocking_findings(repo: str, number: int) -> list[Finding]:
    """Findings from unresolved review threads whose first comment carries a blocking marker."""
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
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in threads.get("nodes") or []:
            if node.get("isResolved"):
                continue
            finding = _parse_thread(node)
            if finding:
                found.append(finding)
        page = threads.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return found
        after = page["endCursor"]


def check_runs(repo: str, head_sha: str) -> dict[str, str]:
    """Map check name -> conclusion for check runs and commit statuses on `head_sha`."""
    out: dict[str, str] = {}
    runs = _gh_json("api", f"repos/{repo}/commits/{head_sha}/check-runs", "-F", "per_page=100")
    for cr in (runs or {}).get("check_runs") or []:
        out[cr["name"]] = cr.get("conclusion") or cr.get("status") or "unknown"
    status = _gh_json("api", f"repos/{repo}/commits/{head_sha}/status", "-F", "per_page=100")
    for st in (status or {}).get("statuses") or []:
        out.setdefault(st["context"], st.get("state") or "unknown")
    return out


def readiness(
    repo: str, pr: PR, required_ci: tuple[str, ...], reviewed_head: str | None
) -> Readiness:
    """Decide whether `pr` may be marked ready / merged. Never raises on policy failures."""
    reasons: list[str] = []
    if pr.state != "OPEN":
        reasons.append(f"PR #{pr.number} is {pr.state}, not open")
    if not reviewed_head:
        reasons.append("no review recorded for the current head")
    elif pr.head_sha != reviewed_head:
        reasons.append(f"head {pr.head_sha[:12]} differs from reviewed {reviewed_head[:12]}")
    blocking = open_blocking_findings(repo, pr.number)
    for f in blocking:
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
    """Merge PR `number` at exactly `head_sha` and return the merge commit SHA."""
    if strategy not in ("squash", "merge", "rebase"):
        raise MergeFailed(("gh", "pr", "merge"), f"unknown merge strategy: {strategy}")
    argv = (
        "pr", "merge", str(number), "--repo", repo, f"--{strategy}",
        "--match-head-commit", head_sha, "--delete-branch",
    )
    try:
        _gh(*argv, timeout=300)
    except GhError as exc:
        raise MergeFailed(exc.command, exc.tail) from exc
    view = _gh_json("pr", "view", str(number), "--repo", repo, "--json", "mergeCommit,merged")
    merged = (view or {}).get("merged")
    sha = ((view or {}).get("mergeCommit") or {}).get("oid")
    if not merged or not sha:
        raise MergeFailed(("gh", *argv), f"PR #{number} not verified merged: {view!r}")
    return sha
