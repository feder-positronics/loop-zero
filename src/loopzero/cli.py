"""Command line: composes the modules into start, check, pr, review, ready, merge, status."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import json
import random
import re
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from loopzero import _proc, eligibility, github, mergify, runners, sandbox, worktree
from loopzero import config as config_mod
from loopzero.types import CheckReport, CheckResult, Config, Finding, LoopZeroError, ReviewResult

CHECKS_FILE = Path(".loopzero") / "checks.json"
GIT_TIMEOUT = 60.0
REVIEW_MARKER_RE = eligibility.REVIEW_MARKER_RE
RUNNER_FAILURES = (runners.RunnerMissing, runners.RunnerAuthFailed, runners.RunnerBadOutput)
HANDLED = (LoopZeroError, worktree.WorktreeError, github.GhError, OSError, ValueError)
UNEXPECTED = (KeyError, TypeError)
PAGE = 100
# --wait: poll interval, default timeout, and how long a required check may stay
# missing before GitHub is assumed to have dropped the event for this head.
# Each poll costs several GitHub API calls from a quota shared by every session.
WAIT_INTERVAL, WAIT_TIMEOUT, MISSING_RUN_GRACE = 30.0, 1800.0, 120.0
PUSH_TIMEOUT = 600.0  # consumer pre-push hooks (lint, tests) routinely exceed GIT_TIMEOUT
NEXT_WAIT = "next: loopzero ready --wait"  # named at the moment of need; agents hand-roll polls otherwise
_sleep = time.sleep
OFFLINE_FAILURE_RE = re.compile(
    r"failed to fetch|dns error|network is unreachable|temporary failure in name resolution|"
    r"name or service not known|could not fetch url|newconnectionerror|err_pnpm_.*fetch|"
    r"network request failed|max retries exceeded with url|readtimeouterror|enetunreach|"
    r"econnrefused|getaddrinfo (?:enotfound|eai_again)|could not resolve host",
    re.IGNORECASE,
)


class CliError(LoopZeroError):
    """A refused command; the message says what to do instead."""


class ReviewersUnavailable(CliError):
    """All reviewer failures, retained as separate diagnostic lines."""


class IndependentReviewerUnavailable(ReviewersUnavailable):
    """No independent configured model family can provide the review."""


def review_marker(head: str, kind: str, source: str = "model") -> str:
    return f"<!-- loopzero:review v=1 head={head} kind={kind} source={source} -->"


# --------------------------------------------------------------------------- git / context


def _git(cwd: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    done = _proc.run(["git", *args], cwd=cwd, env_allowlist=worktree.GIT_ENV, timeout=timeout)
    if done.exit_code != 0:
        raise CliError(f"git {' '.join(args)} failed: {_proc.tail(done.stderr, 1)}")
    return done.stdout.strip()


def _toplevel() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel"))


def _repo_root(wt: Path) -> Path:
    """The verified primary checkout or bare repository owning this linked worktree."""
    first = _git(wt, "worktree", "list", "--porcelain", "-z").split("\0", 1)[0]
    if not first.startswith("worktree "):
        raise CliError("Git did not identify the primary worktree; refusing cleanup")
    root = Path(first.removeprefix("worktree "))
    common_args = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if (root.resolve() == wt.resolve()
            or Path(_git(root, *common_args)).resolve() != Path(_git(wt, *common_args)).resolve()):
        raise CliError("primary repository does not own this linked worktree; refusing cleanup")
    return root


def _load_config(args: argparse.Namespace, root: Path, *, pin_checks: bool = False) -> Config:
    path = args.config or root / "workflow.toml"
    if not pin_checks:
        return config_mod.load(path)
    if args.config:
        print("checks are unpinned because --config was provided")
        return config_mod.load(path)
    config = config_mod.load(path, checks={})
    base_checks = config_mod.load_base(root, config.base_branch)
    if base_checks is None:
        return config_mod.load(path)
    config = config_mod.load(path, checks=base_checks)
    delivery = config_mod.load_base(root, config.base_branch, "delivery") or {}
    config = dataclasses.replace(config, review_publishers=config_mod._strings(
        "delivery.review_publishers", delivery.get("review_publishers", [])))
    if args.command == "check":
        revision = config_mod.base_revision(root, config.base_branch)
        print(f"checks pinned to origin/{config.base_branch}@{revision}")
    return config


def _context(args: argparse.Namespace, *, pin_checks: bool = False) -> tuple[Path, Config]:
    wt = _toplevel()
    return wt, _load_config(args, wt, pin_checks=pin_checks)


def _is_ancestor(wt: Path, sha: str, head: str) -> bool:
    done = _proc.run(
        ["git", "merge-base", "--is-ancestor", sha, head],
        cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    )
    return done.exit_code == 0


def _primary_base(wt: Path, config: Config) -> str:
    base = config_mod.base_revision(wt, config.base_branch)
    done = _proc.run(
        ["git", "merge-base", base, "HEAD"],
        cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    )
    return done.stdout.strip() if done.exit_code == 0 else base


def _require_clean(wt: Path) -> None:
    if worktree.is_dirty(wt):
        raise CliError("worktree has uncommitted or untracked changes; commit or remove them first")


def _require_task_branch(config: Config, branch: str) -> None:
    if branch == config.base_branch or not branch.startswith("lz/"):
        raise CliError(
            f"branch {branch!r} is not a loopzero task branch (lz/<slug>); run from a worktree "
            "created by `loopzero start`"
        )


def _require_pr(config: Config, branch: str, *, allow_merged: bool = False) -> github.PR:
    pr = github.pr_for_branch(config.repo, branch)
    if pr is None:
        raise CliError(f"no pull request for {branch}; run `loopzero pr` first")
    if pr.state != "OPEN" and not (allow_merged and pr.state == "MERGED"):
        raise CliError(f"PR #{pr.number} is {pr.state}, not open: {pr.url}")
    return pr


def _require_pushed(pr: github.PR, head: str) -> None:
    if pr.head_sha != head:
        raise CliError(
            f"PR head {pr.head_sha[:12]} differs from local {head[:12]}; push or pull first"
        )


# --------------------------------------------------------------------------- review markers


def _pr_reviews(repo: str, number: int) -> list[dict]:
    """All reviews on the PR, oldest first, following pages until a short one."""
    reviews: list[dict] = []
    page = 1
    while True:
        endpoint = f"repos/{repo}/pulls/{number}/reviews?per_page={PAGE}&page={page}"
        batch = list(github.api_get(endpoint) or [])
        reviews += batch
        if len(batch) < PAGE:
            return reviews
        page += 1


@dataclasses.dataclass(frozen=True)
class Marker:
    head: str
    kind: str
    state: str
    verdict: str  # "approve" | "request_changes" | ""


def _lineage_markers(wt: Path, reviews: list[dict], head: str,
                     publishers: tuple[str, ...] = ()) -> list[Marker]:
    """Trusted publisher markers on their named commits within this head's ancestry."""
    found: list[Marker] = []
    trusted = {login.casefold() for login in publishers}
    for review in reviews:
        body = review.get("body") or ""
        match = REVIEW_MARKER_RE.search(body)
        if not match or review.get("commit_id") != match.group(1):
            continue
        if not trusted:
            trusted = {github.login().casefold()}
        if (review.get("user") or {}).get("login", "").casefold() not in trusted:
            continue
        if not _is_ancestor(wt, match.group(1), head):
            continue
        state = review.get("state") or ""
        verdict = ""
        if state == "CHANGES_REQUESTED" or "**request_changes**" in body:
            verdict = "request_changes"
        elif state == "APPROVED" or "**approve**" in body:
            verdict = "approve"
        found.append(Marker(match.group(1), match.group(2), state, verdict))
    return found


def _decide_kind(markers: list[Marker], head: str) -> tuple[str, str | None]:
    """Return (kind, reviewed_head) or raise when the lineage's review budget is spent."""
    primaries = [m for m in markers if m.kind == "primary"]
    deltas = [m for m in markers if m.kind == "delta"]
    if not primaries:
        return "primary", None
    if deltas:
        raise CliError(
            "review budget exhausted for this lineage (primary and delta already posted); "
            "a fresh lineage is for one class-wide repair, not the next finding (see "
            "resolve-findings): audit the class, then rewrite the reviewed commits "
            "(squash/amend) so they are no longer ancestors of HEAD and run review again"
        )
    if len(primaries) > 1:
        raise CliError("more than one primary review marker found for this lineage; refusing")
    reviewed = primaries[0].head
    if head.startswith(reviewed) or reviewed.startswith(head):
        raise CliError(f"head {head[:12]} already has a primary review; push new commits first")
    return "delta", reviewed


def _readiness(
    wt: Path, config: Config, pr: github.PR, head: str, reviews: list[dict] | None = None,
    *, workflow_wait: bool = False,
) -> github.Readiness:
    review = eligibility.eligible_review(
        reviews if reviews is not None else _pr_reviews(config.repo, pr.number),
        head, config.review_publishers or (github.login(),),
    )
    review_at = review.get("submitted_at") if review else None
    # GitHub's UTC submission time predates this process; monotonic time cannot date it.
    review_age = (time.time() - datetime.strptime(review_at, "%Y-%m-%dT%H:%M:%SZ")
                  .replace(tzinfo=UTC).timestamp()) if review_at else None
    result = github.readiness(
        config.repo, pr, config.base_branch, config.required_ci,
        head if review else None, review_at=review_at,
        review_grace=review_age is not None and 0 <= review_age < MISSING_RUN_GRACE,
        workflow_wait=workflow_wait,
    )
    report = _load_report(wt)
    if report is None or report.head != head or report.dirty or not report.ok:
        result = github.Readiness(
            ready=False, reasons=(*result.reasons, "run loopzero check at this head")
        )
    if config.merge_strategy == "mergify" and pr.state == "OPEN":
        behind = f"PR #{pr.number} is behind {pr.base_ref}; rebase and rerun checks"
        reasons = result.reasons
        if mergify.configured(config.repo, config.base_branch, config.mergify_queue or ""):
            reasons = tuple(reason for reason in reasons if reason != behind)
        else:
            reasons = (*reasons, "Mergify queue configuration is unverified or permits in-place head updates")
        result = github.Readiness(not reasons, reasons, result.waitable_failures)
    return result


# --------------------------------------------------------------------------- checks report


def _save_report(wt: Path, report: CheckReport) -> None:
    path = wt / CHECKS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "head": report.head,
        "dirty": report.dirty,
        "results": [dataclasses.asdict(result) for result in report.results],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _load_report(wt: Path) -> CheckReport | None:
    path = wt / CHECKS_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        results = tuple(CheckResult(**item) for item in data["results"])
        dirty = data.get("dirty", True)
        if not isinstance(dirty, bool):
            raise TypeError("dirty must be a bool")
        return CheckReport(head=str(data["head"]), dirty=dirty, results=results)
    except (TypeError, KeyError, ValueError) as exc:
        print(f"warning: ignoring unreadable {path}: {exc}", file=sys.stderr)
        return None


def _validation_section(wt: Path, head: str) -> str:
    loaded = _load_report(wt)
    if loaded is None:
        return "(no `loopzero check` run recorded)"
    recorded, results = loaded.head, loaded.results
    lines = [f"Recorded for `{recorded[:12]}`" + ("" if recorded == head else " (not current head)")]
    lines += [
        f"- `{r.command}`: exit {r.exit_code}" for r in results
    ] or ["- (no check commands configured)"]
    return "\n".join(lines)


_CHECKS_START = "<!-- loopzero:checks:start -->"
_CHECKS_END = "<!-- loopzero:checks:end -->"
_LEGACY_CHECKS = re.compile(
    r"^Recorded for `[0-9a-f]+`(?: \(not current head\))?\n"
    r"(?:- `[^\n]+`: exit -?\d+ \(\d+\.\d+s\)\n|"
    r"- \(no check commands configured\)\n)*",
    re.MULTILINE,
)


def _pr_body(wt: Path, head: str, existing: str | None = None) -> str:
    """Refresh only generated checks; the live body owns existing PR prose."""
    body = (existing if existing is not None else worktree.task_text(wt)).replace("\r\n", "\n")
    block = f"{_CHECKS_START}\n{_validation_section(wt, head)}\n{_CHECKS_END}"
    pattern = re.compile(r"^## (?:Validation|Checks)\n(.*?)(?=^## |\Z)",
                         re.DOTALL | re.MULTILINE)
    match = pattern.search(body)
    if match is None:
        return f"{body.rstrip()}\n\n## Validation\n{block}\n"
    content = match.group(1)
    if _CHECKS_START in content or _CHECKS_END in content:
        if (content.count(_CHECKS_START) != 1 or content.count(_CHECKS_END) != 1
                or content.index(_CHECKS_START) > content.index(_CHECKS_END)):
            raise CliError("malformed loopzero checks block in PR Validation; repair its markers")
        start = content.index(_CHECKS_START)
        end = content.index(_CHECKS_END) + len(_CHECKS_END)
        content = content[:start] + block + content[end:]
    else:
        # Migrate our old report/placeholder only, retaining behavioral evidence.
        content = _LEGACY_CHECKS.sub("", content)
        content = re.sub(
            r"^\((?:filled by `loopzero check`|no `loopzero check` run recorded)\)\n?",
            "", content, flags=re.MULTILINE,
        )
        content = block + "\n" + ("\n" + content.lstrip("\n") if content.strip() else "\n")
    return body[:match.start()] + "## Validation\n" + content + body[match.end():]


def _sync_task_narrative(wt: Path, existing: str) -> str:
    """Explicit pr updates task-owned sections; live evidence/reviews stay authoritative."""
    pattern = re.compile(r"^## ([^\n]+)\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    task = worktree.task_text(wt).replace("\r\n", "\n")
    existing = existing.replace("\r\n", "\n")
    owned = {m.group(1): m.group() for m in pattern.finditer(task)
             if m.group(1) not in {"Validation", "Checks", "Review"}}
    first = pattern.search(existing)
    task_first = pattern.search(task)
    prefix = task[:task_first.start()] if task_first else task
    content = existing[first.start():] if first else ""

    def replace(match: re.Match[str]) -> str:
        replacement = owned.pop(match.group(1), None)
        if replacement is None:
            return match.group()
        trailing = match.group()[len(match.group().rstrip()):]
        return replacement.rstrip() + trailing

    content = pattern.sub(replace, content)
    for section in owned.values():
        content = content.rstrip() + "\n\n" + section
    return prefix + content.lstrip("\n")


def _update_pr_checks(wt: Path, head: str, config: Config, pr: github.PR) -> None:
    body = _pr_body(wt, head, pr.body)
    if body != (pr.body or "").replace("\r\n", "\n"):
        github.update_body(config.repo, pr.number, body)


_REQUIRED_TASK_LINES = ("Context", "Problem", "Goal")


def _require_pr_details(wt: Path) -> None:
    """Refuse an unfilled Context/Problem/Goal line before the branch is pushed."""
    task = worktree.task_text(wt)
    for name in _REQUIRED_TASK_LINES:
        match = re.search(rf"^- \*\*{name}:\*\*[ \t]*([^\r\n]*)$", task, re.MULTILINE)
        value = match.group(1).strip() if match else ""
        if not value or re.search(r"<[^>]+>", value):
            raise CliError(f"fill '- **{name}:**' in {worktree.task_file(wt)}")


def _append_review(body: str, result: ReviewResult) -> str:
    """Append one compact review record under Review, replacing its placeholder."""
    line = f"- {result.kind}, {result.family}, {result.head[:12]}, {result.verdict}"
    pattern = re.compile(r"(^## Review\n)(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)
    match = pattern.search(body)
    if not match:
        return f"{body.rstrip()}\n\n## Review\n{line}\n"
    existing = match.group(2).rstrip()
    if existing.startswith("(filled by `loopzero review`)"):
        existing = ""
    replacement = match.group(1) + (f"{existing}\n" if existing else "") + line + "\n\n"
    return pattern.sub(lambda _: replacement, body, count=1).rstrip() + "\n"


def _refresh_pr_checks(wt: Path, config: Config) -> None:
    """Best-effort refresh of an existing PR after the branch has an upstream."""
    argv = ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    upstream = _proc.run(argv, cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT)
    if upstream.exit_code != 0:
        return
    try:
        branch, head = worktree.branch(wt), worktree.head(wt)
        pr = github.pr_for_branch(config.repo, branch)
        if pr is not None and pr.state == "OPEN":
            _update_pr_checks(wt, head, config, pr)
    except (github.GhError, OSError, ValueError, CliError) as exc:
        print(f"note: could not refresh PR checks: {_one_line(exc)}", file=sys.stderr)


_SECTIONS = {
    "objective", "context and goal", "acceptance", "base", "checks", "validation",
    "review", "notes",
}


def _pr_title(wt: Path, branch: str) -> str:
    """First heading of task.md that is not a template section name; else the branch."""
    headings = [ln.lstrip("#").strip() for ln in worktree.task_text(wt).splitlines() if ln[:1] == "#"]
    return next((t for t in headings if t and t.lower() not in _SECTIONS), branch)


# --------------------------------------------------------------------------- commands


def cmd_start(args: argparse.Namespace) -> int:
    root = _toplevel()
    argv = ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]
    current = _proc.run(argv, cwd=root, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT)
    if current.stdout.strip().startswith("lz/"):
        raise CliError(f"already inside task worktree {root}; run start from the main checkout")
    config = _load_config(args, root)
    print(worktree.start(root, args.slug, config.base_branch))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    try:
        report = sandbox.run_checks(config, wt)
    except sandbox.SandboxUnavailable as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return 2
    for result in report.results:
        print(f"exit {result.exit_code:<3} {result.duration_s:7.1f}s  {result.command}")
    for result in report.results:
        if result.exit_code != 0:
            print(f"--- {result.command} (exit {result.exit_code}) ---", file=sys.stderr)
            tail = _proc.tail(result.tail)
            print(tail, file=sys.stderr)
            if not config.network and OFFLINE_FAILURE_RE.search(tail):
                env = {
                    **sandbox.SANDBOX_ENV,
                    **_proc.build_env(config.env_allowlist),
                    **dict(config.env),
                }
                print(
                    "\nThe sandbox has no network because `[checks] network = false`; "
                    f"the effective UV_CACHE_DIR inside the sandbox is {env['UV_CACHE_DIR']}. "
                    "To prepare offline, run the same commands once on the host so the cache "
                    "under `[checks] writable` is warm, or set `[checks] env` UV_CACHE_DIR to "
                    "that cache.",
                    file=sys.stderr,
                )
    _save_report(wt, report)
    _refresh_pr_checks(wt, config)
    print("PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


def cmd_pr(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    _require_pr_details(wt)
    pr = github.pr_for_branch(config.repo, branch)
    force_lease = pr is not None and pr.state == "OPEN" and not _is_ancestor(wt, pr.head_sha, head)
    push = ["push", "-u", "origin"]
    if force_lease:
        # Only rewrite history this worktree has seen; an unknown remote head is someone else's.
        known = _proc.run(["git", "cat-file", "-e", f"{pr.head_sha}^{{commit}}"], cwd=wt,
                          env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT)
        if known.exit_code != 0:
            raise CliError(
                f"PR head {pr.head_sha[:12]} is not in this worktree; fetch and integrate it first"
            )
        push.append(f"--force-with-lease={branch}:{pr.head_sha}")
    push.append(branch)
    try:
        _git(wt, *push, timeout=PUSH_TIMEOUT)
    except CliError as exc:
        if force_lease:
            raise CliError(f"{exc}; remote moved since PR lookup; fetch and retry") from exc
        raise
    if pr is not None and pr.state == "OPEN":
        body = _pr_body(wt, head, _sync_task_narrative(wt, pr.body or ""))
        if body != (pr.body or "").replace("\r\n", "\n"):
            github.update_body(config.repo, pr.number, body)
    else:
        pr = github.create_draft_pr(
            config.repo, branch, config.base_branch, _pr_title(wt, branch), _pr_body(wt, head)
        )
    print(pr.url)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    pr = _require_pr(config, branch)
    _require_pushed(pr, head)
    if pr.base_ref != config.base_branch:
        raise CliError(f"PR targets {pr.base_ref}, configured base is {config.base_branch}")
    if config.review_publishers and github.login().casefold() not in {
        login.casefold() for login in config.review_publishers
    }:
        raise CliError("current GitHub login is not a configured review publisher")
    kind, reviewed = _decide_kind(
        _lineage_markers(wt, _pr_reviews(config.repo, pr.number), head, config.review_publishers), head
    )
    if args.repost:
        author = runners.author_family(wt, head)
        excluded = [family for family in config.reviewers if family == author]
        if not any(family != author for family in config.reviewers):
            raise IndependentReviewerUnavailable(_independence_message(author, excluded, []))
        result = _load_review(wt, head, kind)
        sessions = result.provenance["session_ids"]
        prefix = review_marker(head, kind, "repost") + "\n" + (
            "Reposted from runner session" + ("s" if len(sessions) != 1 else "")
            + ": " + ", ".join(f"`{session}`" for session in sessions)
        )
    else:
        try:
            result = _run_review(wt, config, head, kind, reviewed, args.model, args.effort)
        except sandbox.SandboxUnavailable as exc:
            print(f"sandbox unavailable: {exc}", file=sys.stderr)
            return 2
        final_head, final_dirty = worktree.head(wt), worktree.is_dirty(wt)
        if final_head != head or final_dirty:
            raise CliError(
                f"worktree changed during review: HEAD before {head}, HEAD after {final_head}; "
                f"dirty before False, dirty after {final_dirty}"
            )
        _save_review(wt, result)
        prefix = review_marker(head, kind, "model")
    try:
        github.post_review(
            config.repo, pr.number, head, result, body_prefix=prefix, pr=pr
        )
    except github.GhError as exc:
        path = _review_file(wt, head, kind)
        raise CliError(
            f"{exc} | review saved at {path}; fix the cause and run `loopzero review --repost`"
        ) from exc
    try:
        github.update_body(config.repo, pr.number, _append_review(pr.body or _pr_body(wt, head), result))
    except (github.GhError, OSError, ValueError) as exc:
        print(f"note: could not append PR review summary: {_one_line(exc)}", file=sys.stderr)
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in runners.SEVERITIES}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    print(f"{kind} review by {result.family} on {head[:12]}: {result.verdict} ({summary})")
    return 0 if result.verdict == "approve" else 5  # the exit code is the verdict, as for check


def _run_review(
    wt: Path, config: Config, head: str, kind: str, reviewed: str | None,
    model: str | None = None, effort: str | None = None,
) -> ReviewResult:
    since = reviewed if kind == "delta" else _primary_base(wt, config)
    diff = worktree.diff_since(wt, since)
    task = worktree.task_text(wt)
    author = runners.author_family(wt, head)
    candidates = [family for family in config.reviewers if family != author]
    failures: list[str] = []
    result = None
    excluded = [family for family in config.reviewers if family == author]
    if not candidates:
        raise IndependentReviewerUnavailable(_independence_message(author, excluded, failures))
    selected: dict[str, tuple[str | None, str | None]] = {}
    for family in candidates:  # validate every candidate before launching any reviewer
        settings = config.review.get(family)
        chosen = effort or (settings.effort if settings else None)
        if settings and chosen and settings.allowed_efforts and chosen not in settings.allowed_efforts:
            raise CliError(
                f"effort {chosen!r} is not allowed for {family}; "
                f"expected one of {settings.allowed_efforts}"
            )
        selected[family] = (model or (settings.model if settings else None), chosen)
    for family in candidates:
        selected_model, selected_effort = selected[family]
        try:
            result = runners.review_with(
                family, cwd=wt, head=head, kind=kind, diff=diff, task_text=task,
                reviewer_ro_paths=config.reviewer_ro_paths,
                model=selected_model, effort=selected_effort,
            )
            break
        except runners.RunnerPromptTooLong:
            chunks = runners.split_diff(diff, config.review_chunk_bytes)
            print(
                f"reviewer {family} rejected the full diff; reviewing {len(chunks)} chunks",
                file=sys.stderr,
            )
            chunk_results = [
                runners.review_with(
                    family, cwd=wt, head=head, kind=kind, diff=chunk, task_text=task,
                    reviewer_ro_paths=config.reviewer_ro_paths,
                    model=selected_model, effort=selected_effort,
                )
                for chunk in chunks
            ]
            result = runners.merge_reviews(chunk_results)
            break
        except RUNNER_FAILURES as exc:
            reason = str(exc).splitlines()[0]
            detail = "\n".join(str(exc).splitlines()[:3])
            if isinstance(exc, runners.RunnerAuthFailed):
                login = "claude auth login" if family == "claude" else "codex login"
                detail += f"\nFix: run `{login}`."
            elif isinstance(exc, runners.RunnerMissing):
                package = "@anthropic-ai/claude-code" if family == "claude" else "@openai/codex"
                detail += f"\nFix: install it with `npm install -g {package}`."
            failures.append(detail)
            print(f"reviewer {family} unavailable, trying next: {reason}", file=sys.stderr)
    if result is None:
        raise IndependentReviewerUnavailable(_independence_message(author, excluded, failures))
    return result


def _independence_message(author: str | None, excluded: list[str], failures: list[str]) -> str:
    identity = author or "unknown"
    excluded_text = ", ".join(excluded) if excluded else "none"
    detail = ("\n\nFailures:\n" + "\n\n".join(failures)) if failures else ""
    return (
        f"no independent reviewer can run (author family: {identity}; excluded families: "
        f"{excluded_text}). Owner options: configure another family, log in, or record an "
        "explicit owner-approved exception in the PR by a human. Delivery remains blocked; "
        f"--repost does not apply.{detail}"
    )


def _review_file(wt: Path, head: str, kind: str) -> Path:
    return wt / ".loopzero" / f"review-{head[:12]}-{kind}.json"


def _save_review(wt: Path, result: ReviewResult) -> None:
    path = _review_file(wt, result.head, result.kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(result), indent=2) + "\n")


def _load_review(wt: Path, head: str, kind: str) -> ReviewResult:
    path = _review_file(wt, head, kind)
    if not path.exists():
        raise CliError(f"no saved {kind} review for {head[:12]} at {path}; run without --repost")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise CliError(f"{path} is not runner-produced: invalid saved review") from exc
    if not isinstance(data, dict):
        raise CliError(f"{path} is not runner-produced: invalid saved review")
    if data.get("head") != head:
        raise CliError(f"{path} holds a review of {str(data.get('head'))[:12]}, not HEAD {head[:12]}")
    try:
        findings = tuple(Finding(**f) for f in data["findings"])
        result = ReviewResult(**{**data, "findings": findings})
    except (KeyError, TypeError, ValueError) as exc:
        raise CliError(f"{path} is not runner-produced: invalid saved review") from exc
    if not runners.validate_runner_result(result):
        raise CliError(f"{path} is not runner-produced: provenance/envelope mismatch")
    return result


def _pr_readiness(
    wt: Path, config: Config, *, allow_merged: bool = False, workflow_wait: bool = False
) -> tuple[github.PR, github.Readiness | None]:
    """PR plus readiness; readiness is None only for an already merged PR (allow_merged)."""
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    pr = _require_pr(config, branch, allow_merged=allow_merged)
    _require_pushed(pr, head)
    if pr.state == "MERGED":
        return pr, None
    return pr, _readiness(wt, config, pr, head, workflow_wait=workflow_wait)


def _cleanup(wt: Path) -> None:
    root = _repo_root(wt)
    try:
        worktree.cleanup(root, wt)
    except (worktree.WorktreeError, OSError) as exc:
        print(f"warning: merged but worktree cleanup failed: {exc}", file=sys.stderr)
    print(f"cd {root}")


def cmd_ready(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    pr, readiness = _pr_readiness(wt, config, workflow_wait=args.wait is not None)
    assert readiness is not None
    waiting = _draft_checks_waiting(config, pr, readiness)
    if waiting:
        github.mark_ready(config.repo, pr.number)
        print(f"marked ready; waiting for required checks: {', '.join(waiting)}")
        if args.wait is None:
            print(f"waiting for required checks; {NEXT_WAIT}")
            return 3
    if args.wait is not None and (waiting or _pending_checks(config, pr, readiness)):
        waited = _wait_for_checks(wt, config, pr, args.wait, kicked=bool(waiting))
        if waited is None:
            print(f"timed out waiting for required checks on {pr.head_sha[:12]}")
            return 3
        pr, readiness = waited
    for reason in readiness.reasons:
        print(f"not ready: {reason}")
    if not readiness.ready:
        if args.wait is None and _pending_checks(config, pr, readiness):
            print(f"not ready: required checks pending or missing; {NEXT_WAIT}")
        return 1
    if pr.is_draft and not waiting:  # `waiting` means this run already marked it ready
        github.mark_ready(config.repo, pr.number)
    print(f"ready: {pr.url}")
    return 0


def _draft_checks_waiting(
    config: Config, pr: github.PR, readiness: github.Readiness
) -> tuple[str, ...]:
    if not pr.is_draft or not readiness.reasons:
        return ()
    expected: dict[str, str] = {}
    for name in config.required_ci:
        expected[f"required check '{name}' missing on {pr.head_sha[:12]}"] = name
        expected[f"required check '{name}' is skipped"] = name
    if not all(reason in expected for reason in readiness.reasons):
        return ()
    blocked = {expected[reason] for reason in readiness.reasons}
    return tuple(name for name in config.required_ci if name in blocked)


def _pending_checks(
    config: Config, pr: github.PR, readiness: github.Readiness, kicked: bool = False
) -> tuple[str, ...]:
    """Reasons worth waiting on: every blocker is a required check that may still turn green.

    `kicked` also accepts `skipped`: this run just marked the draft ready, and the
    skipped result belongs to the draft until GitHub replaces it.
    """
    states = [f"missing on {pr.head_sha[:12]}", "is pending", *(["is skipped"] if kicked else [])]
    waitable = {f"required check '{n}' {state}" for n in config.required_ci for state in states}
    waitable.update(readiness.waitable_failures)
    reasons = readiness.reasons
    return reasons if reasons and all(r in waitable for r in reasons) else ()


def _poll_until(started: float, timeout: float) -> Iterator[None]:
    delay = WAIT_INTERVAL
    while True:
        yield  # Observe immediately, including --wait=0, and once at the deadline.
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return
        _sleep(min(remaining, delay * random.uniform(0.9, 1.0)))
        delay = min(120.0, delay * 2)


def _wait_for_checks(
    wt: Path, config: Config, pr: github.PR, timeout: float, *, kicked: bool
) -> tuple[github.PR, github.Readiness] | None:
    """Poll readiness for the same head until only non-waitable state remains; None on timeout."""
    started, shown = time.monotonic(), None
    for _ in _poll_until(started, timeout):
        current = github.pr_view(config.repo, pr.number)
        if current.head_sha != pr.head_sha:
            raise CliError(
                f"PR head changed while waiting: {pr.head_sha[:12]} to {current.head_sha[:12]}"
            )
        readiness = _readiness(wt, config, current, pr.head_sha, workflow_wait=True)
        pending = _pending_checks(config, current, readiness, kicked)
        if not pending:
            return current, readiness
        if pending != shown:
            print("waiting: " + "; ".join(pending))
            shown = pending
        elapsed = time.monotonic() - started
        # Missing check runs do not prove that no workflow was scheduled. Diagnose only
        # when no visible pending check explains why the required jobs have not appeared.
        if (
            elapsed >= MISSING_RUN_GRACE
            and all(" missing on " in r for r in pending)
            and "pending" not in github.check_runs(config.repo, pr.head_sha).values()
        ):
            raise CliError(
                f"{'; '.join(pending)} after {elapsed:.0f}s: inspect workflow scheduling, "
                f"triggers and permissions for head {pr.head_sha[:12]}; after correcting the cause, "
                "retry `loopzero ready --wait`"
            )


def _wait_for_merge(config: Config, pr: github.PR, timeout: float) -> str | None:
    """Poll until the queue lands the PR and return the merge SHA; None on timeout."""
    started = time.monotonic()
    print(f"PR #{pr.number} is queued for merge into {config.base_branch}; waiting")
    for _ in _poll_until(started, timeout):
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
        if sha:
            return sha
        if not github.merge_pending(config.repo, pr.number):
            sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
            if sha:
                return sha
            raise CliError(f"PR #{pr.number} left the merge queue unmerged")


def _wait_for_mergify(
    config: Config, pr: github.PR, timeout: float, confirmed: bool
) -> str | None:
    """Follow one exact source head from request through confirmed membership to merge."""
    started = time.monotonic()
    queue = config.mergify_queue
    assert queue is not None
    for _ in _poll_until(started, timeout):
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
        if sha:
            return sha
        if not mergify.configured(config.repo, config.base_branch, queue):
            raise CliError("Mergify queue configuration became unsafe or unavailable while waiting")
        state = mergify.membership(config.repo, pr.number, queue)
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
        if sha:
            return sha
        if state is not None and not confirmed:
            mergify.mark_confirmed(config.repo, pr.number, pr.head_sha, queue)
            confirmed = True
            print(f"PR #{pr.number} confirmed in Mergify queue {queue}; waiting")
        elif state is None and confirmed:
            sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
            if sha:
                return sha
            raise CliError(f"PR #{pr.number} left Mergify queue {queue} unmerged; "
                           f"fix the cause and explicitly requeue with @mergifyio queue {queue}")


def _merge_with_mergify(
    wt: Path, config: Config, branch: str, pr: github.PR,
    readiness: github.Readiness, timeout: float | None,
) -> str | None:
    """Request Mergify after source gates pass; BEHIND is admission state, not readiness."""
    behind = f"PR #{pr.number} is behind {pr.base_ref}; rebase and rerun checks"
    blockers = tuple(reason for reason in readiness.reasons if reason != behind)
    if blockers:
        raise CliError("not ready to request Mergify: " + "; ".join(blockers))
    queue = config.mergify_queue
    assert queue is not None
    if not mergify.configured(config.repo, config.base_branch, queue):
        raise CliError("Mergify queue configuration is unverified or permits in-place head updates")
    requested, confirmed_marker = mergify.markers(
        config.repo, pr.number, pr.head_sha, queue
    )
    state = mergify.membership(config.repo, pr.number, queue)
    current = _require_pr(config, branch, allow_merged=True)
    if current.head_sha != pr.head_sha:
        raise CliError(
            f"PR head changed during Mergify lookup: {pr.head_sha[:12]} to {current.head_sha[:12]}"
        )
    confirmed = state is not None
    if confirmed and not confirmed_marker:
        mergify.mark_confirmed(config.repo, pr.number, pr.head_sha, queue)
        confirmed_marker = True
    if not confirmed and confirmed_marker:
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
        if sha:
            return sha
        raise CliError(f"PR #{pr.number} left Mergify queue {queue} unmerged; "
                       f"fix the cause and explicitly requeue with @mergifyio queue {queue}")
    if not confirmed and not requested:
        mergify.request(config.repo, pr.number, pr.head_sha, queue)
        requested = True
    if timeout is not None:
        return _wait_for_mergify(config, pr, timeout, confirmed)
    state_name = "confirmed in" if confirmed else "requested from"
    print(
        f"PR #{pr.number} {state_name} Mergify queue {queue}; "
        "next: loopzero merge --wait follows the queue, verifies and cleans up"
    )
    return None


def cmd_merge(args: argparse.Namespace) -> int:
    if args.cancel and args.wait is not None:
        raise CliError("merge --cancel cannot be combined with --wait")
    wt, config = _context(args, pin_checks=True)
    branch = worktree.branch(wt)
    if args.cancel:
        if config.merge_strategy != "mergify":
            raise CliError("merge --cancel is only available with delivery.merge = 'mergify'")
        pr = _require_pr(config, branch)
        mergify.dequeue(config.repo, pr.number)
        print(f"requested removal of PR #{pr.number} from Mergify")
        return 0
    pr, readiness = _pr_readiness(wt, config, allow_merged=True)
    if readiness is None:
        sha = github.merged_sha(
            config.repo, pr.number,
            expected_head=pr.head_sha,
        )
        if sha is None:
            raise CliError(f"PR #{pr.number} reports MERGED but has no merge commit yet; rerun")
        print(f"PR #{pr.number} was already merged as {sha[:12]}; cleaning up")
        print(sha)
        _delete_remote_branch(config, branch)
        _cleanup(wt)
        return 0
    if config.merge_strategy == "mergify":
        sha = _merge_with_mergify(wt, config, branch, pr, readiness, args.wait)
        if sha is None:
            if args.wait is not None:
                print(f"timed out waiting for Mergify to land PR #{pr.number}")
                return 3
            return 0
        print(sha)
        _delete_remote_branch(config, branch)
        _cleanup(wt)
        return 0
    if not readiness.ready:
        hint = f"; {NEXT_WAIT}" if _pending_checks(config, pr, readiness) else ""
        raise CliError("not ready to merge: " + "; ".join(readiness.reasons) + hint)
    sha = github.merge(config.repo, pr.number, config.merge_strategy, pr.head_sha)
    if sha is None and args.wait is not None:
        sha = _wait_for_merge(config, pr, args.wait)
        if sha is None:
            print(f"timed out waiting for the merge queue to land PR #{pr.number}")
            return 3
    if sha is None:
        print(
            f"PR #{pr.number} is queued for merge into {config.base_branch}; "
            "next: loopzero merge --wait follows the queue, verifies and cleans up"
        )
        return 0
    print(sha)
    _delete_remote_branch(config, branch)
    _cleanup(wt)
    return 0


def _delete_remote_branch(config: Config, branch: str) -> None:
    try:
        github.delete_remote_branch(config.repo, branch)
    except (github.GhError, _proc.ProcTimeout) as exc:
        print(f"warning: merged but remote branch cleanup failed for {branch}: {exc}", file=sys.stderr)


def cmd_resolve(args: argparse.Namespace) -> int:
    """List open blocking findings, or reply to one with what changed and resolve its thread."""
    wt, config = _context(args)
    pr = _require_pr(config, worktree.branch(wt))
    if not args.finding:
        for marker_id, _thread, f in github.open_findings(config.repo, pr.number):
            print(f"{marker_id}  {f.severity}  {f.path or '-'}:{f.line or '-'}  {f.title}")
        return 0
    if not args.reply.strip():
        raise CliError("a reply saying what changed (or why it does not apply) is required")
    finding = github.resolve_finding(config.repo, pr.number, args.finding, args.reply)
    print(f"resolved {args.finding}: {finding.title}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    wt = _toplevel()
    if args.path:
        print(wt)
        return 0
    config = _load_config(args, wt, pin_checks=True)
    branch, head = worktree.branch(wt), worktree.head(wt)
    print(f"worktree: {wt}")
    print(f"branch:   {branch}")
    print(f"head:     {head}")
    print(f"base:     {config.base_branch}")
    print(f"dirty:    {'yes' if worktree.is_dirty(wt) else 'no'}")
    pr = github.pr_for_branch(config.repo, branch)
    if pr is None:
        print("pr:       none (run `loopzero pr`)")
        return 0
    print(f"pr:       #{pr.number} {pr.url} ({'draft' if pr.is_draft else 'ready'}, {pr.state})")
    reviews = _pr_reviews(config.repo, pr.number)
    markers = _lineage_markers(wt, reviews, head, config.review_publishers)
    if markers:
        latest = markers[-1]
        print(f"review:   {latest.kind} on {latest.head[:12]} ({latest.state.lower() or 'posted'})")
    else:
        print("review:   none for this lineage")
    readiness = _readiness(wt, config, pr, head, reviews)
    print(f"ready:    {'yes' if readiness.ready else 'no'}")
    for reason in readiness.reasons:
        print(f"  - {reason}")
    if config.merge_strategy == "mergify" and pr.state == "OPEN" and pr.head_sha == head:
        queue = config.mergify_queue
        assert queue is not None
        requested, confirmed = mergify.markers(config.repo, pr.number, head, queue)
        state = mergify.membership(config.repo, pr.number, queue)
        label = "confirmed" if state else "ejected" if confirmed else "requested" if requested else "none"
        print(f"mergify:  {label} ({queue})")
    return 0


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopzero", description="One task, one branch, one draft PR, one review, one merge."
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="path to workflow.toml (default: repo root)"
    )
    parser.add_argument("--version", action="version", version=_version())
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="create an owned worktree and branch for <slug>")
    start.add_argument("slug")
    start.set_defaults(func=cmd_start)
    sub.add_parser("check", help="run the configured checks in the sandbox").set_defaults(
        func=cmd_check
    )
    sub.add_parser("pr", help="push and open or refresh the draft PR").set_defaults(func=cmd_pr)
    review = sub.add_parser("review", help="run one model review and post it on the PR")
    review.add_argument(
        "--repost", action="store_true", help="post the saved review for HEAD without a model"
    )
    review.add_argument("--model", help="reviewer model override for this run")
    review.add_argument("--effort", help="reviewer effort override for this run")
    review.set_defaults(func=cmd_review)
    ready = sub.add_parser(
        "ready", help="compute readiness and mark the PR ready; --wait polls required checks"
    )
    merge = sub.add_parser(
        "merge", help="recheck readiness, merge, remove the worktree; --wait follows a merge queue"
    )
    for waiter, what in ((ready, "required checks on this head"), (merge, "the merge queue")):
        waiter.add_argument(
            "--wait", nargs="?", const=WAIT_TIMEOUT, type=float, metavar="SECONDS",
            help=f"wait for {what} (default {WAIT_TIMEOUT:.0f}s); exit 3 on timeout",
        )
    merge.add_argument(
        "--cancel", action="store_true", help="request removal from the configured Mergify queue"
    )
    ready.set_defaults(func=cmd_ready)
    merge.set_defaults(func=cmd_merge)
    resolve = sub.add_parser("resolve", help="list open findings, or reply to one and resolve it")
    resolve.add_argument("finding", nargs="?", help="finding id from the list (8 hex digits)")
    resolve.add_argument("reply", nargs="?", default="", help="what changed, e.g. the fix commit")
    resolve.set_defaults(func=cmd_resolve)
    status = sub.add_parser("status", help="show where this branch is in the sequence")
    status.add_argument("--path", action="store_true", help="print only the worktree path")
    status.set_defaults(func=cmd_status)
    return parser


def _version() -> str:
    """Package version plus the pinned source revision when installed from Git."""
    dist = importlib.metadata.distribution("loopzero")
    try:
        commit = json.loads(dist.read_text("direct_url.json") or "{}")["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        commit = "unknown revision (not installed from a Git URL)"
    return f"loopzero {dist.version} {commit}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except IndependentReviewerUnavailable as exc:
        print(f"loopzero {args.command}: {exc}", file=sys.stderr)
        return 4
    except HANDLED as exc:
        message = str(exc) if isinstance(exc, ReviewersUnavailable) else _one_line(exc)
        print(f"loopzero {args.command}: {message}", file=sys.stderr)
        return 1
    except UNEXPECTED as exc:
        print(f"loopzero {args.command}: unexpected response: {_one_line(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"loopzero {args.command}: interrupted", file=sys.stderr)
        return 130


def _one_line(exc: BaseException) -> str:
    return " | ".join(ln.strip() for ln in (str(exc) or type(exc).__name__).splitlines() if ln.strip())


if __name__ == "__main__":
    sys.exit(main())
