"""Command line: composes the modules into start, check, pr, review, ready, merge, status."""

from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import re
import sys
from pathlib import Path

from loopzero import _proc, github, runners, sandbox, worktree
from loopzero import config as config_mod
from loopzero.types import CheckReport, CheckResult, Config, LoopZeroError

CHECKS_FILE = Path(".loopzero") / "checks.json"
GIT_TIMEOUT = 60.0
REVIEW_MARKER_RE = re.compile(
    r"<!--\s*loopzero:review\s+head=([0-9a-fA-F]{7,40})\s+kind=(primary|delta)\s*-->"
)
RUNNER_FAILURES = (runners.RunnerMissing, runners.RunnerAuthFailed, runners.RunnerBadOutput)
HANDLED = (LoopZeroError, worktree.WorktreeError, github.GhError, OSError, ValueError)
UNEXPECTED = (KeyError, TypeError)
PAGE = 100


class CliError(LoopZeroError):
    """A refused command; the message says what to do instead."""


def review_marker(head: str, kind: str) -> str:
    return f"<!-- loopzero:review head={head} kind={kind} -->"


# --------------------------------------------------------------------------- git / context


def _git(cwd: Path, *args: str) -> str:
    done = _proc.run(
        ["git", *args], cwd=cwd, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT
    )
    if done.exit_code != 0:
        raise CliError(f"git {' '.join(args)} failed: {_proc.tail(done.stderr, 1)}")
    return done.stdout.strip()


def _toplevel() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel"))


def _repo_root(wt: Path) -> Path:
    """The main checkout that owns this worktree (parent of the common .git directory)."""
    return Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def _load_config(args: argparse.Namespace, root: Path) -> Config:
    return config_mod.load(args.config or root / "workflow.toml")


def _context(args: argparse.Namespace) -> tuple[Path, Config]:
    wt = _toplevel()
    return wt, _load_config(args, wt)


def _is_ancestor(wt: Path, sha: str, head: str) -> bool:
    done = _proc.run(
        ["git", "merge-base", "--is-ancestor", sha, head],
        cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    )
    return done.exit_code == 0


def _primary_base(wt: Path, config: Config) -> str:
    done = _proc.run(
        ["git", "merge-base", f"origin/{config.base_branch}", "HEAD"],
        cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    )
    return done.stdout.strip() if done.exit_code == 0 else worktree.base_sha(wt)


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


def _gh_api(endpoint: str) -> object:
    """GET `endpoint` through `gh api` and decode the JSON response."""
    argv = ["gh", "api", endpoint]
    try:
        done = _proc.run(argv, cwd=Path.cwd(), env_allowlist=github.GH_ENV, timeout=120)
    except _proc.ToolMissing as exc:
        raise github.GhMissing(tuple(argv), str(exc)) from exc
    if done.exit_code != 0:
        raise github.GhError(tuple(argv), done.stderr or done.stdout)
    try:
        return json.loads(done.stdout) if done.stdout.strip() else None
    except json.JSONDecodeError as exc:
        raise github.GhError(tuple(argv), f"invalid JSON from gh: {done.stdout[-600:]}") from exc


def _pr_reviews(repo: str, number: int) -> list[dict]:
    """All reviews on the PR, oldest first, following pages until a short one."""
    reviews: list[dict] = []
    page = 1
    while True:
        batch = list(_gh_api(f"repos/{repo}/pulls/{number}/reviews?per_page={PAGE}&page={page}") or [])
        reviews += batch
        if len(batch) < PAGE:
            return reviews
        page += 1


@functools.cache
def _token_login() -> str:
    """Login of the account behind the `gh` token; cached for the duration of one command."""
    login = (_gh_api("user") or {}).get("login")
    if not login:
        raise CliError("could not determine the gh token's login (`gh api user`)")
    return str(login)


@dataclasses.dataclass(frozen=True)
class Marker:
    head: str
    kind: str
    state: str
    verdict: str  # "approve" | "request_changes" | ""


def _lineage_markers(wt: Path, reviews: list[dict], head: str) -> list[Marker]:
    """Markers posted by this token, on the commit they name, whose head is an ancestor of `head`.

    A marker is only trusted when the review's `commit_id` equals the marker head and the
    review author is the token login; anything else could be hand-posted.
    """
    found: list[Marker] = []
    for review in reviews:
        body = review.get("body") or ""
        match = REVIEW_MARKER_RE.search(body)
        if not match or review.get("commit_id") != match.group(1):
            continue
        if (review.get("user") or {}).get("login") != _token_login():
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
            "rewrite the reviewed commits (squash/amend) so they are no longer ancestors of "
            "HEAD, then run review again"
        )
    if len(primaries) > 1:
        raise CliError("more than one primary review marker found for this lineage; refusing")
    reviewed = primaries[0].head
    if head.startswith(reviewed) or reviewed.startswith(head):
        raise CliError(f"head {head[:12]} already has a primary review; push new commits first")
    return "delta", reviewed


def _readiness(wt: Path, config: Config, pr: github.PR, head: str) -> github.Readiness:
    """`github.readiness` plus: a request_changes review with no blocking thread is not ready."""
    markers = _lineage_markers(wt, _pr_reviews(config.repo, pr.number), head)
    latest = markers[-1] if markers else None
    result = github.readiness(config.repo, pr, config.required_ci, latest.head if latest else None)
    if latest and latest.verdict == "request_changes" and not any(
        r.startswith("open ") for r in result.reasons
    ):
        reason = f"latest {latest.kind} review requested changes but posted no blocking findings"
        result = github.Readiness(ready=False, reasons=(*result.reasons, reason))
    return result


# --------------------------------------------------------------------------- checks report


def _save_report(wt: Path, report: CheckReport) -> None:
    path = wt / CHECKS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "head": report.head,
        "results": [dataclasses.asdict(result) for result in report.results],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _load_report(wt: Path) -> tuple[str, tuple[CheckResult, ...]] | None:
    path = wt / CHECKS_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        results = tuple(CheckResult(**item) for item in data["results"])
        return str(data["head"]), results
    except (TypeError, KeyError, ValueError) as exc:
        print(f"warning: ignoring unreadable {path}: {exc}", file=sys.stderr)
        return None


def _checks_section(wt: Path, head: str) -> str:
    loaded = _load_report(wt)
    if loaded is None:
        return "(no `loopzero check` run recorded)"
    recorded, results = loaded
    lines = [f"Recorded for `{recorded[:12]}`" + ("" if recorded == head else " (not current head)")]
    lines += [
        f"- `{r.command}`: exit {r.exit_code} ({r.duration_s:.1f}s)" for r in results
    ] or ["- (no check commands configured)"]
    return "\n".join(lines)


def _pr_body(wt: Path, head: str) -> str:
    task = worktree.task_text(wt).rstrip()
    task = re.sub(r"\n## Checks\n.*?(?=\n## |\Z)", "", task, flags=re.DOTALL).rstrip()
    return f"{task}\n\n## Checks\n{_checks_section(wt, head)}\n"


def _pr_title(wt: Path, branch: str) -> str:
    first = worktree.task_text(wt).strip().splitlines()[:1]
    title = first[0].lstrip("# ").strip() if first else ""
    return title or branch


# --------------------------------------------------------------------------- commands


def cmd_start(args: argparse.Namespace) -> int:
    root = _toplevel()
    current = _proc.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    ).stdout.strip()
    if current.startswith("lz/"):
        raise CliError(f"already inside task worktree {root} ({current}); run start from the main checkout")
    config = _load_config(args, root)
    print(worktree.start(root, args.slug, config.base_branch))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    try:
        report = sandbox.run_checks(config, wt)
    except sandbox.SandboxUnavailable as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return 2
    for result in report.results:
        print(f"exit {result.exit_code:<3} {result.duration_s:7.1f}s  {result.command}")
    _save_report(wt, report)
    print("PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


def cmd_pr(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    _git(wt, "push", "-u", "origin", branch)
    body = _pr_body(wt, head)
    pr = github.pr_for_branch(config.repo, branch)
    if pr is not None and pr.state == "OPEN":
        github.update_body(config.repo, pr.number, body)
    else:
        pr = github.create_draft_pr(
            config.repo, branch, config.base_branch, _pr_title(wt, branch), body
        )
    print(pr.url)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    pr = _require_pr(config, branch)
    _require_pushed(pr, head)
    kind, reviewed = _decide_kind(
        _lineage_markers(wt, _pr_reviews(config.repo, pr.number), head), head
    )
    since = reviewed if kind == "delta" else _primary_base(wt, config)
    diff = worktree.diff_since(wt, since)
    task = worktree.task_text(wt)
    author = runners.author_family(wt, head)
    first = runners.pick_reviewer(config.reviewers, author)
    failures: list[str] = []
    result = None
    for family in [first, *(f for f in config.reviewers if f != first)]:
        if family == author:
            print(f"note: reviewer {family} shares the author's model family", file=sys.stderr)
        try:
            result = runners.review_with(
                family, cwd=wt, head=head, kind=kind, diff=diff, task_text=task
            )
            break
        except RUNNER_FAILURES as exc:
            reason = str(exc).splitlines()[0]
            failures.append(reason)
            print(f"reviewer {family} unavailable, trying next: {reason}", file=sys.stderr)
    if result is None:
        raise CliError("every configured reviewer failed: " + "; ".join(failures))
    github.post_review(
        config.repo, pr.number, head, result, body_prefix=review_marker(head, kind)
    )
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in runners.SEVERITIES}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    print(f"{kind} review by {result.family} on {head[:12]}: {result.verdict} ({summary})")
    return 0


def _pr_readiness(
    wt: Path, config: Config, *, allow_merged: bool = False
) -> tuple[github.PR, github.Readiness | None]:
    """PR plus readiness; readiness is None only for an already merged PR (allow_merged)."""
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    pr = _require_pr(config, branch, allow_merged=allow_merged)
    _require_pushed(pr, head)
    if pr.state == "MERGED":
        return pr, None
    return pr, _readiness(wt, config, pr, head)


def _cleanup(wt: Path) -> None:
    root = _repo_root(wt)
    try:
        worktree.cleanup(root, wt)
    except (worktree.WorktreeError, OSError) as exc:
        print(f"warning: merged but worktree cleanup failed: {exc}", file=sys.stderr)
    print(f"cd {root}")


def cmd_ready(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    pr, readiness = _pr_readiness(wt, config)
    assert readiness is not None
    for reason in readiness.reasons:
        print(f"not ready: {reason}")
    if not readiness.ready:
        return 1
    if pr.is_draft:
        github.mark_ready(config.repo, pr.number)
    print(f"ready: {pr.url}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    pr, readiness = _pr_readiness(wt, config, allow_merged=True)
    if readiness is None:
        print(f"PR #{pr.number} was already merged externally at {pr.head_sha[:12]}; cleaning up")
        _cleanup(wt)
        return 0
    if not readiness.ready:
        raise CliError("not ready to merge: " + "; ".join(readiness.reasons))
    sha = github.merge(config.repo, pr.number, config.merge_strategy, pr.head_sha)
    print(sha)
    _cleanup(wt)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    wt = _toplevel()
    if args.path:
        print(wt)
        return 0
    config = _load_config(args, wt)
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
    markers = _lineage_markers(wt, _pr_reviews(config.repo, pr.number), head)
    if markers:
        latest = markers[-1]
        print(f"review:   {latest.kind} on {latest.head[:12]} ({latest.state.lower() or 'posted'})")
    else:
        print("review:   none for this lineage")
    readiness = _readiness(wt, config, pr, head)
    print(f"ready:    {'yes' if readiness.ready else 'no'}")
    for reason in readiness.reasons:
        print(f"  - {reason}")
    return 0


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopzero", description="One task, one branch, one draft PR, one review, one merge."
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="path to workflow.toml (default: repo root)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="create an owned worktree and branch for <slug>")
    start.add_argument("slug")
    start.set_defaults(func=cmd_start)
    sub.add_parser("check", help="run the configured checks in the sandbox").set_defaults(
        func=cmd_check
    )
    sub.add_parser("pr", help="push and open or refresh the draft PR").set_defaults(func=cmd_pr)
    sub.add_parser("review", help="run one model review and post it on the PR").set_defaults(
        func=cmd_review
    )
    sub.add_parser("ready", help="compute readiness and mark the PR ready").set_defaults(
        func=cmd_ready
    )
    sub.add_parser("merge", help="recheck readiness, merge, remove the worktree").set_defaults(
        func=cmd_merge
    )
    status = sub.add_parser("status", help="show where this branch is in the sequence")
    status.add_argument("--path", action="store_true", help="print only the worktree path")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _token_login.cache_clear()
    try:
        return args.func(args)
    except HANDLED as exc:
        print(f"loopzero {args.command}: {_one_line(exc)}", file=sys.stderr)
        return 1
    except UNEXPECTED as exc:
        print(f"loopzero {args.command}: unexpected response: {_one_line(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"loopzero {args.command}: interrupted", file=sys.stderr)
        return 130


def _one_line(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


if __name__ == "__main__":
    sys.exit(main())
