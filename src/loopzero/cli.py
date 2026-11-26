"""Command line: composes the modules into start, check, pr, review, ready, merge, status."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import importlib.metadata
import io
import json
import math
import os
import shlex
import random
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial, wraps
from pathlib import Path

from loopzero import _proc, eligibility, github, mergify, runners, sandbox, worktree
from loopzero import config as config_mod
from loopzero.types import CheckReport, CheckResult, Config, Finding, LoopZeroError, ReviewResult, is_check_exit_code

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
STALE_VERDICT_GRACE, MAX_REVIEW_CLOCK_SKEW = 900.0, 30.0
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


def _git_push(wt: Path, args: list[str], *, force_lease: bool) -> None:
    done = _proc.run(["git", *args], cwd=wt, env_allowlist=worktree.GIT_ENV,
                     extra_env={"LC_ALL": "C"}, timeout=PUSH_TIMEOUT)
    if done.exit_code != 0:
        detail = _proc.tail(done.stderr)[-2000:]
        if force_lease and any(line.startswith("!\t") and line.endswith("\t[rejected] (stale info)")
                               for line in done.stdout.splitlines()):
            detail += "\nremote moved since PR lookup; fetch and retry"
        raise CliError(f"git {' '.join(args)} failed: {detail.strip()}")


def _toplevel() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel"))


def _repo_root(wt: Path) -> Path:
    """The verified primary checkout or bare repository owning this linked worktree."""
    first = _git(wt, "worktree", "list", "--porcelain", "-z").split("\0", 1)[0]
    if not first.startswith("worktree "):
        raise CliError("Git did not identify the primary worktree; refusing cleanup")
    root = Path(first.removeprefix("worktree "))
    common = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if root.resolve() == wt.resolve() or common not in (root.resolve(), (root / ".git").resolve()):
        raise CliError("primary repository does not own this linked worktree; refusing cleanup")
    return root


def _load_config(args: argparse.Namespace, root: Path, *, pin_checks: bool = False) -> Config:
    path = args.config or root / "workflow.toml"
    if not pin_checks:
        return config_mod.load(path)
    if args.config:
        print("checks are unpinned because --config was provided")
        return config_mod.load(path)
    config = config_mod.load_trusted(root)
    if args.command == "check":
        if not config.checks:
            raise config_mod.ConfigError(
                f"origin/{config.base_branch}:workflow.toml: checks.commands must list "
                "at least one command to run checks"
            )
        print(f"checks pinned to origin/{config.base_branch}@{config.trusted_base_revision}")
    return config


def _context(args: argparse.Namespace, *, pin_checks: bool = False) -> tuple[Path, Config]:
    wt = _toplevel()
    return wt, _load_config(args, wt, pin_checks=pin_checks)


def _is_ancestor(wt: Path, sha: str, head: str) -> bool:
    done = _proc.run(
        ["git", "merge-base", "--is-ancestor", sha, head],
        cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    )
    if done.exit_code not in (0, 1):
        detail = _proc.tail(done.stderr or done.stdout, 1)
        raise CliError(f"git merge-base --is-ancestor failed (exit {done.exit_code}): "
                       f"{detail or 'retry the ancestry check'}")
    return done.exit_code == 0


def _primary_base(wt: Path, config: Config) -> str:
    base = config.trusted_base_revision or config_mod.base_revision(wt, config.base_branch)
    done = _proc.run(
        ["git", "merge-base", base, "HEAD"],
        cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
    )
    if done.exit_code == 0:
        return done.stdout.strip()
    if done.exit_code != 1:
        raise CliError(f"git merge-base failed: {_proc.tail(done.stderr or done.stdout, 1)}")
    message = f"no merge base between HEAD and the configured base {config.base_branch!r}"
    if _git(wt, "rev-parse", "--is-shallow-repository") == "true":
        message += ". To review, deepen or unshallow the checkout and retry"
    raise CliError(message)


def _require_clean(wt: Path) -> None:
    if worktree.is_dirty(wt):
        raise CliError("worktree has uncommitted or untracked changes; commit or remove them first")


def _require_task_branch(config: Config, branch: str) -> None:
    if branch == config.base_branch:
        raise CliError(
            f"branch {branch!r} is the configured base; use a task branch"
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


def _rest_rows(endpoint: str, what: str) -> list[dict]:
    """Every row of a REST list, oldest first, following pages until a short one."""
    rows: list[dict] = []
    for page in range(1, 101):
        batch = github.api_get(f"{endpoint}?per_page={PAGE}&page={page}")
        if not isinstance(batch, list) or not all(isinstance(row, dict) for row in batch):
            raise CliError(f"invalid GitHub {what} response")
        rows += batch
        if len(batch) < PAGE:
            return rows
    raise CliError(f"GitHub {what} pagination limit exceeded")


def _pr_reviews(repo: str, number: int) -> list[dict]:
    return _rest_rows(f"repos/{repo}/pulls/{number}/reviews", "reviews")


@dataclasses.dataclass(frozen=True)
class Marker:
    head: str
    kind: str
    state: str


def _pr_markers(reviews: list[dict], head: str,
                publishers: tuple[str, ...] = (), *, repo: str = "", number: int = 0) -> list[Marker]:
    """Trusted publisher markers on their named commits across the PR."""
    found: list[Marker] = []
    trusted = {login.casefold() for login in publishers}
    for review in reviews:
        body = review.get("body") or ""
        if not eligibility.review_projection(review, head, repo, number)[0] and any(
                runners.codex_fatal_startup(item) for item in eligibility.review_envelopes(body)[1]):
            continue
        match = eligibility.leading_marker(body)
        if not match or review.get("commit_id") != match.group(1):
            continue
        if not trusted:
            trusted = {github.login().casefold()}
        if (review.get("user") or {}).get("login", "").casefold() not in trusted:
            continue
        found.append(Marker(match.group(1), match.group(2), review.get("state") or ""))
    return found


def _review_cap_spent(markers: list[Marker]) -> bool:
    return len({m.head for m in markers}) >= 2 or {m.kind for m in markers} >= {"primary", "delta"}


def _decide_kind(markers: list[Marker], head: str) -> tuple[str, str | None]:
    """Return (kind, reviewed_head) or raise when the PR's review budget is spent."""
    if _review_cap_spent(markers):
        raise CliError(
            "review cap reached (2 rounds per PR): fix or reject the findings "
            "(resolve-findings) and run `loopzero ready`, or split the PR"
        )
    primary = next((m for m in markers if m.kind == "primary"), None)
    if primary is None:
        return "primary", None
    reviewed = primary.head
    if head.startswith(reviewed) or reviewed.startswith(head):
        raise CliError(f"head {head[:12]} already has a primary review; run `loopzero ready`, or push fixes for the delta")
    return "delta", reviewed


MERGEABILITY_PENDING = "GitHub is computing mergeability"


def _readiness(
    wt: Path, config: Config, pr: github.PR, head: str, reviews: list[dict] | None = None,
    *, workflow_wait: bool = False, announce_receipt: bool = False,
) -> github.Readiness:
    if "hold" in pr.labels:
        reason = f"PR #{pr.number} {github.MERGE_HOLD_MESSAGE}"
        return github.Readiness(ready=False, reasons=(reason,))
    if reviews is None:
        reviews = _pr_reviews(config.repo, pr.number)
    publishers = config.review_publishers or (github.login(),)
    review = eligibility.readiness_review(
        reviews, head, publishers, repo=config.repo, number=pr.number, base_branch=config.base_branch,
    )
    review_at = review.get("submitted_at") if review else None
    # GitHub's review timestamp is in UTC and predates this process.
    review_age = (time.time() - datetime.strptime(review_at, "%Y-%m-%dT%H:%M:%SZ")
                  .replace(tzinfo=UTC).timestamp()) if review_at else None
    result = github.readiness(
        config.repo, pr, config.base_branch, config.required_ci,
        head if review else None, review_at=review_at,
        ancestor_review=review is not None and review["commit_id"] != head,
        review_grace=review_age is not None and 0 <= review_age < MISSING_RUN_GRACE,
        workflow_wait=workflow_wait,
        # Allow skew only for verdicts GitHub timestamps establish are stale.
        stale_verdict_grace=(
            workflow_wait and review_age is not None
            and -MAX_REVIEW_CLOCK_SKEW <= review_age < STALE_VERDICT_GRACE
        ),
    )
    report = _load_report(wt)
    receipt_problem = _check_report_problem(report, wt, config, head)
    if receipt_problem:
        result = github.Readiness(
            ready=False,
            reasons=(*result.reasons, receipt_problem),
        )
    elif announce_receipt:
        _print_receipt(wt, report, "accepted")
    if config.merge_strategy == "mergify" and pr.state == "OPEN":
        behind = f"PR #{pr.number} is behind {pr.base_ref}; rebase and rerun checks"
        reasons = result.reasons
        if mergify.configured(config.repo, config.base_branch, config.mergify_queue or ""):
            reasons = tuple(reason for reason in reasons if reason != behind)
        else:
            reasons = (*reasons, "Mergify queue configuration is unverified or permits in-place head updates")
        result = dataclasses.replace(result, ready=not reasons, reasons=reasons)
    if result.reasons == (github.MISSING_REVIEW_REASON,):
        markers = _pr_markers(reviews, head, publishers, repo=config.repo, number=pr.number)
        if _review_cap_spent(markers) and all(marker.head != head for marker in markers):
            result = dataclasses.replace(result, reasons=(
                f"review cap spent (primary + delta) and {head[:12]} has no eligible review. "
                "Open a new PR for the changes that need review",
            ))
    if pr.state == "OPEN" and pr.mergeable == "UNKNOWN":
        result = dataclasses.replace(result, ready=False, reasons=(*result.reasons, MERGEABILITY_PENDING))
    return result


# --------------------------------------------------------------------------- checks report


def _check_report_problem(
    report: CheckReport | None, wt: Path, config: Config, head: str, *, require_pass: bool = True,
) -> str | None:
    if (report is None or report.head != head or report.dirty or (require_pass and not report.ok)
            or tuple(result.command for result in report.results) != config.checks):
        return "run loopzero check at this head"
    recovery = ("Run loopzero check in this worktree at this head with the same current CLI and effective "
                "settings as ready, using a local coordinator for delegated stages.")
    if report.settings_sha256 is None:
        return f"check receipt is missing its settings fingerprint. {recovery}"
    if report.settings_sha256 != sandbox.settings_fingerprint(config, wt):
        return f"check receipt has an incompatible settings fingerprint. {recovery}"
    return None


def _current_check_report(wt: Path, config: Config, head: str) -> CheckReport | None:
    report = _load_report(wt)
    return None if _check_report_problem(report, wt, config, head) else report


def _print_receipt(wt: Path, report: CheckReport, action: str) -> None:
    """Best-effort transcript identity; never authority or environment attestation."""
    try:
        content = _git(wt, "rev-parse", f"{report.head}^{{tree}}").strip()
        commands = json.dumps([r.command for r in report.results], separators=(",", ":"))
        digest = hashlib.sha256(commands.encode()).hexdigest()
        print(f"receipt {action}: head={report.head} content={content} "
              f"config=commands-sha256:{digest} environment=unknown")
    except Exception:  # output must never affect the verdict
        return


@contextmanager
def _lock_task_state(wt: Path) -> Iterator[None]:
    directory = wt / ".loopzero"
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save_report(wt: Path, report: CheckReport) -> str | None:
    """Publish the receipt or return the current HEAD if the result is stale."""
    path = wt / CHECKS_FILE
    payload = {
        "head": report.head,
        "dirty": report.dirty,
        "results": [{k: v for k, v in dataclasses.asdict(result).items()
                     if k not in {"input_key", "reused_from"} or v is not None}
                    for result in report.results],
    }
    if report.settings_sha256 is not None:
        payload["settings_sha256"] = report.settings_sha256
    with _lock_task_state(wt):
        current_head = worktree.head(wt)
        if report.head != current_head:
            return current_head
        _write_json_atomically(path, payload)
    return None


def _write_json_atomically(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as stream:
        temp = Path(stream.name)
        try:
            stream.write(json.dumps(payload, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _load_report(wt: Path) -> CheckReport | None:
    path = wt / CHECKS_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        results = tuple(CheckResult(**item) for item in data["results"])
        if any(not is_check_exit_code(result.exit_code) for result in results):
            raise TypeError("exit_code must be an integer")
        dirty = data.get("dirty", True)
        if not isinstance(dirty, bool):
            raise TypeError("dirty must be a bool")
        return CheckReport(head=str(data["head"]), dirty=dirty, results=results,
                           settings_sha256=data.get("settings_sha256"))
    except (TypeError, KeyError, ValueError) as exc:
        print(f"warning: ignoring unreadable {path}: {exc}", file=sys.stderr)
        return None


def _validation_section(wt: Path, head: str, config: Config) -> str:
    loaded = _load_report(wt)
    if loaded is None:
        return "(no `loopzero check` run recorded)"
    recorded, results = loaded.head, loaded.results
    lines = [f"Recorded for `{recorded[:12]}`" + ("" if recorded == head else " (not current head)")]
    if _check_report_problem(loaded, wt, config, head, require_pass=False):
        lines[0] += " (unverified, run `loopzero check`)"
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


def _pr_body(wt: Path, head: str, config: Config, existing: str | None = None) -> str:
    """Refresh only generated checks; the live body owns existing PR prose."""
    body = (existing if existing is not None else worktree.task_text(wt)).replace("\r\n", "\n")
    block = f"{_CHECKS_START}\n{_validation_section(wt, head, config)}\n{_CHECKS_END}"
    pattern = re.compile(r"^## (?:Validation|Checks)\n(.*?)(?=^## |\Z)",
                         re.DOTALL | re.MULTILINE)
    match = pattern.search(body)
    if match is None:
        return _validate_pr_body_size(f"{body.rstrip()}\n\n## Validation\n{block}\n")
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
    return _validate_pr_body_size(body[:match.start()] + "## Validation\n" + content + body[match.end():])


def _validate_pr_body_size(body: str) -> str:
    limit = 65_536
    if len(body) > limit:
        raise CliError(
            f"generated PR body is {len(body)} characters; GitHub allows {limit}. "
            "Shorten the PR description or validation text and rerun the command."
        )
    return body


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
    body = _pr_body(wt, head, config, pr.body)
    if body != (pr.body or "").replace("\r\n", "\n"):
        github.update_body(config.repo, pr.number, body)


_REQUIRED_TASK_LINES = ("Context", "Problem", "Goal")


def _require_pr_details(wt: Path) -> None:
    """Report every unfilled PR description field before push."""
    task = worktree.task_text(wt)
    unfilled = []
    for name in _REQUIRED_TASK_LINES:
        match = re.search(rf"^- \*\*{name}:\*\*[ \t]*(.*(?:\n[ \t]+\S.*)*)", task, re.MULTILINE)
        value = match.group(1).strip() if match else ""
        if not value or re.fullmatch(r"<[^<>]*>", value):
            unfilled.append(f"'- **{name}:**'")
    if unfilled:
        raise CliError(
            f"fill {', '.join(unfilled)} in {worktree.task_file(wt)}"
            " (Context, Problem and Goal become the PR description)"
        )


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
    """Best-effort refresh only when local HEAD is the existing PR head."""
    argv = ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    try:
        upstream = _proc.run(argv, cwd=wt, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT)
        if upstream.exit_code != 0:
            return
        branch, head = worktree.branch(wt), worktree.head(wt)
        pr = github.pr_for_branch(config.repo, branch, open_only=True)
        if pr is not None and pr.head_sha == head:
            _update_pr_checks(wt, head, config, pr)
    except (github.GhError, _proc.ProcTimeout, OSError, ValueError, CliError) as exc:
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
    if current.stdout.strip().startswith("lz/") or (
        current.exit_code == 1 and worktree.is_linked_checkout(root)
    ):
        raise CliError(f"already inside task worktree {root}; run start from the main checkout")
    config = _load_config(args, root)
    print(worktree.start(root, args.slug, config.base_branch))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    if not config.checks:
        raise config_mod.ConfigError("checks.commands must list at least one command to run checks")
    try:
        report = sandbox.run_checks(config, wt, progress=lambda command: print(
            f"running: {command}", flush=True),
            status=lambda message: print(message, flush=True),
            checkpoint=lambda partial: _save_report(wt, partial))
    except sandbox.SandboxUnavailable as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return 2
    for result in report.results:
        if result.exit_code != 0:
            print(f"--- {result.command} (exit {result.exit_code}) ---", file=sys.stderr)
            tail = _proc.tail(result.tail)
            print(tail, file=sys.stderr)
            if not config.network and (result.command.startswith("uv sync --offline --locked")
                                       or OFFLINE_FAILURE_RE.search(tail)):
                env = {
                    **sandbox.SANDBOX_ENV,
                    **_proc.build_env(config.env_allowlist),
                    **dict(config.env),
                }
                print(
                    "\nThe sandbox has no network because `[checks] network = false`; "
                    f"the effective UV_CACHE_DIR inside the sandbox is {env['UV_CACHE_DIR']}. "
                    "To prepare offline, run UV_CACHE_DIR=<effective-cache> uv sync --group dev "
                    "(use the groups your checks require) on the host, or the same commands, so the cache "
                    "under `[checks] writable` is warm, or set `[checks] env` UV_CACHE_DIR to "
                    "that cache.",
                    file=sys.stderr,
                )
    current_head = _save_report(wt, report)
    if current_head is not None:
        print(f"check result discarded: checked HEAD {report.head}, current HEAD {current_head}",
              file=sys.stderr)
        return 1
    if report.ok and not report.dirty:
        _print_receipt(wt, report, "produced")
    _refresh_pr_checks(wt, config)
    print("PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


def cmd_pr(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    _require_pr_details(wt)
    pr = github.merged_pr_for_branch(config.repo, branch)
    if pr is None:
        pr = github.pr_for_branch(config.repo, branch, open_only=True)
    if pr is not None and pr.state == "MERGED":
        raise CliError(
            f"task branch {branch} already merged as PR #{pr.number} ({pr.url}); "
            "run `loopzero start <new-task-slug>` for a new task"
        )
    existing = _sync_task_narrative(wt, pr.body or "") if pr is not None else None
    body = _pr_body(wt, head, config, existing)
    force_lease = False
    push = ["push", "-u", "origin"]
    if pr is not None and pr.state == "OPEN":
        # Only rewrite history this worktree has seen; an unknown remote head is someone else's.
        known = _proc.run(["git", "cat-file", "-e", f"{pr.head_sha}^{{commit}}"], cwd=wt,
                          env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT)
        if known.exit_code != 0:
            raise CliError(
                f"PR head {pr.head_sha[:12]} is not in this worktree; fetch and integrate it first"
            )
        force_lease = not _is_ancestor(wt, pr.head_sha, head)
        if force_lease:
            push.extend(("--porcelain", f"--force-with-lease={branch}:{pr.head_sha}"))
    push.append(branch)
    _git_push(wt, push, force_lease=force_lease)
    if pr is not None and pr.state == "OPEN":
        if body != (pr.body or "").replace("\r\n", "\n"):
            github.update_body(config.repo, pr.number, body)
    else:
        pr = github.create_draft_pr(
            config.repo, branch, config.base_branch, _pr_title(wt, branch), body
        )
    print(pr.url)
    return 0


# Published fallback reasons; --repost posts a saved note only in the form a fresh run would publish.
_FALLBACK_REASONS = ("not authenticated", "usage limit", "not installed", "unavailable")


def _published_fallback(note: str, candidates: list[str], excluded: list[str]) -> bool:
    if not candidates:
        return note == "every configured family contributed (" + ", ".join(excluded) + ")"
    prefix = "independent reviewers failed: "
    if not note.startswith(prefix):
        return False
    parts = [part.partition(": ") for part in note[len(prefix):].split("; ")]
    return ([family for family, _sep, _reason in parts] == candidates
            and all(reason in _FALLBACK_REASONS for _family, _sep, reason in parts))


def _require_review_pr(config: Config, branch: str, *, repost: bool) -> github.PR:
    try:
        return _require_pr(config, branch)
    except github.GhError as exc:
        if "HTTP 404" not in exc.tail or exc.command[:2] != ("gh", "api"):
            raise
        endpoint = exc.command[2]
        pulls = f"repos/{config.repo}/pulls"
        retry = "loopzero review --repost" if repost else "loopzero review"
        number = endpoint.removeprefix(f"{pulls}/")
        if endpoint.startswith(f"{pulls}/") and number.isdecimal():
            raise CliError(
                f"Could not read PR #{number}. Verify that PR and its branch {branch!r}, "
                f"then rerun `{retry}`. {exc}"
            ) from exc
        if exc.tail.endswith(github.PR_APP_INSTALLATION_HINT):
            raise CliError(f"{exc}\nThen rerun `{retry}`.") from exc
        raise


def _lock_review(run_review: Callable[[argparse.Namespace, Path, Config], int]):
    @wraps(run_review)
    def locked(args: argparse.Namespace) -> int:
        wt, config = _context(args, pin_checks=True)
        git_dir = _git(wt, "rev-parse", "--absolute-git-dir")
        fd = os.open(git_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CliError("review already running in this worktree") from exc
            return run_review(args, wt, config)
        finally:
            os.close(fd)

    return locked


@_lock_review
def cmd_review(args: argparse.Namespace, wt: Path, config: Config) -> int:
    if args.config:
        runtime_source = "reviewer paths are unpinned because --config was provided"
    else:
        runtime_source = f"reviewer paths pinned to origin/{config.base_branch}@{config.trusted_base_revision}"
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    pr = _require_review_pr(config, branch, repost=args.repost)
    _require_pushed(pr, head)
    if pr.base_ref != config.base_branch:
        raise CliError(f"PR targets {pr.base_ref}, configured base is {config.base_branch}")
    token = github.app_token()  # one identity for the publisher check and the post
    publisher = github.login(token=token)
    if config.review_publishers and publisher.casefold() not in {
        login.casefold() for login in config.review_publishers
    }:
        raise CliError("current GitHub login is not a configured review publisher")
    reviews = [] if args.repost else _pr_reviews(config.repo, pr.number)
    if args.repost:
        saved = [kind for kind in ("primary", "delta") if _review_file(wt, head, kind).exists()]
        if len(saved) > 1:
            raise CliError("multiple saved reviews for HEAD; publication is ambiguous")
        if not saved:
            reviews = _pr_reviews(config.repo, pr.number)
        kind = saved[0] if saved else _decide_kind(
            _pr_markers(reviews, head, config.review_publishers, repo=config.repo, number=pr.number), head
        )[0]
    else:
        kind, reviewed = _decide_kind(
            _pr_markers(reviews, head, config.review_publishers, repo=config.repo, number=pr.number), head
        )
    if args.repost:
        authors = runners.author_families(wt, head, _primary_base(wt, config))
        excluded = [family for family in config.reviewers if family in authors]
        result = _load_review(wt, head, kind)
        if result.fallback and result.family not in excluded:
            raise CliError(
                f"saved review claims a same-family fallback, but {result.family} is not a configured "
                "reviewer family that contributed to this PR lineage; run without --repost"
            )
        if not result.fallback and not any(family not in authors for family in config.reviewers):
            raise IndependentReviewerUnavailable(
                _independence_message(authors, excluded, [])
            )
        if result.family in authors and not result.fallback:
            raise IndependentReviewerUnavailable(
                f"saved reviewer family {result.family} contributed to this PR lineage; "
                "run without --repost to obtain an independent review"
            )
        publication = json.loads(_review_file(wt, head, kind).read_text()).get("publication")
        if publication is not None:
            if (not isinstance(publication, dict) or publication.get("repo") != config.repo
                    or publication.get("number") != pr.number
                    or not isinstance(publication.get("publisher"), str)
                    or not publication["publisher"]):
                raise CliError("saved publication identity differs from this repository/PR")
            confirmed, reviews = _recover_review(wt, config, pr, result,
                                                 publication["publisher"], token)
            if confirmed:
                return _review_verdict(result)
        else:
            reviews = _pr_reviews(config.repo, pr.number)
        # Exact read-back above may confirm an older published note; a new post needs the current form.
        candidates = [family for family in config.reviewers if family not in authors]
        if result.fallback and not _published_fallback(result.fallback, candidates, excluded):
            raise CliError("saved fallback note is not in the published form; run without --repost")
        allowed, _ = _decide_kind(
            _pr_markers(reviews, head, config.review_publishers, repo=config.repo, number=pr.number), head
        )
        if allowed != kind:
            raise CliError("saved review kind differs from the remaining review budget")
    else:
        try:
            result = _run_review(wt, config, head, kind, reviewed, args.model, args.effort,
                                 runtime_source=runtime_source, publication=(config.repo, pr.number, publisher))
        except sandbox.SandboxUnavailable as exc:
            print(f"sandbox unavailable: {exc}", file=sys.stderr)
            return 2
        final_head, final_dirty = worktree.head(wt), worktree.is_dirty(wt)
        if final_head != head or final_dirty:
            raise CliError(
                f"worktree changed during review: HEAD before {head}, HEAD after {final_head}; "
                f"dirty before False, dirty after {final_dirty}"
            )
        prefix = review_marker(head, kind, "model")
    try:
        evidence = runners.codex_evidence(result)
    except runners.RunnerBadOutput:
        _save_review(wt, result)
        raise
    if evidence is not None and (evidence["repo"], evidence["pr"], evidence["publisher"]) != (config.repo, pr.number, publisher):
        raise CliError("native publication context changed; HOLD")
    if args.repost:
        prefix = _repost_prefix(result)
    _save_review(wt, result, {"repo": config.repo, "number": pr.number, "publisher": publisher})
    token = _refresh_publisher_token(token, publisher)
    try:
        github.post_review(
            config.repo, pr.number, head, result, body_prefix=prefix, pr=pr, token=token,
            publisher=publisher,
        )
    except (github.GhError, _proc.ProcTimeout) as exc:
        path = _review_file(wt, head, kind)
        try:
            if _recover_review(wt, config, pr, result, publisher, token)[0]:
                return _review_verdict(result)
        except (github.GhError, CliError, _proc.ProcTimeout) as recovery:
            raise CliError(f"{exc} | publication read-back blocked: {recovery}; "
                           f"review saved at {path}; retry `loopzero review --repost` "
                           "after resolving the read-back failure") from recovery
        raise CliError(
            f"{exc} | publication unconfirmed; review saved at {path}; "
            "inspect the cause and run `loopzero review --repost` to reconcile or explicitly retry"
        ) from exc
    try:
        live = github.pr_view(config.repo, pr.number)
        github.update_body(config.repo, pr.number, _append_review(live.body, result))
    except (github.GhError, _proc.ProcTimeout, OSError, ValueError) as exc:
        print(f"note: could not append PR review summary: {_one_line(exc)}", file=sys.stderr)
    return _review_verdict(result)


def _refresh_publisher_token(token: str, publisher: str) -> str:
    """The review may outlive the app token: re-mint it, but only for the same publisher."""
    if not token or (fresh := github.app_token()) == token:
        return token
    if not fresh or github.login(token=fresh).casefold() != publisher.casefold():
        raise CliError(f"could not refresh the app token for review publisher {publisher}; the review "
                       "is saved: fix the app token helper, then run `loopzero review --repost`")
    return fresh


def _review_verdict(result: ReviewResult) -> int:
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in runners.SEVERITIES}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    print(f"{result.kind} review by {result.family} on {result.head[:12]}: {result.verdict} ({summary})")
    return 0 if result.verdict == "approve" else 5  # the exit code is the verdict, as for check


def _repost_prefix(result: ReviewResult) -> str:
    sessions = result.provenance["session_ids"]
    return review_marker(result.head, result.kind, "repost") + "\n" + (
        "Reposted from runner session" + ("s" if len(sessions) != 1 else "")
        + ": " + ", ".join(f"`{session}`" for session in sessions)
    )


def _recover_review(wt: Path, config: Config, pr: github.PR, result: ReviewResult,
                    publisher: str, token: str) -> tuple[bool, list[dict]]:
    with github.bounded_reads(time.monotonic() + 30, token=token):
        reviews = _pr_reviews(config.repo, pr.number)
        return _confirm_review(wt, config, pr, result, publisher, reviews), reviews


def _review_formats(result: ReviewResult) -> list[tuple[str, github.ReviewSettingsLayout, bool]]:
    old_settings = "requested_settings" not in result.provenance
    formats: list[tuple[github.ReviewSettingsLayout, bool]] = [("current", False)]
    codex = runners.codex_evidence(result)
    if codex is None:
        formats.append(("current", True))
        if old_settings:
            formats.append(("summary", True))
    if result.family == "claude" and not old_settings and "configured_settings" not in result.provenance:
        formats.append(("requested-reported", True))
    if codex is not None and all("configured" not in call for call in codex["calls"]):
        formats.append(("requested-reported", False))
    if result.family == "codex" and old_settings:
        formats.append(("summary", False))
    return [(prefix, settings, display)
            for prefix in (review_marker(result.head, result.kind, "model"), _repost_prefix(result))
            for settings, display in formats]


def _confirm_review(wt: Path, config: Config, pr: github.PR, result: ReviewResult,
                    publisher: str, reviews: list[dict]) -> bool:
    """Confirm an exact live publication; absence authorizes no automatic write."""
    trusted = config.review_publishers or (publisher,)
    evidence = [r for r in reviews if "loopzero:review" in (r.get("body") or "") and (
        r.get("commit_id") == result.head
        or ((marker := eligibility.leading_marker(r["body"])) and marker.group(1) == result.head))]
    fresh = github.pr_view(config.repo, pr.number)
    if fresh.head_sha != result.head or fresh.state != "OPEN" or fresh.base_ref != pr.base_ref:
        raise CliError("PR changed during publication recovery")
    if not evidence:
        return False
    if (publisher.casefold() not in {name.casefold() for name in trusted}
            or eligibility.eligible_review(reviews, result.head, trusted, repo=config.repo, number=pr.number) is None):
        raise CliError("publication is revoked, pending or untrusted")
    payloads = github.review_payloads(config.repo, pr.number, result.head, result, _review_formats(result))
    states = {"COMMENTED", "APPROVED" if result.verdict == "approve" else "CHANGES_REQUESTED"}
    for review in evidence:
        expected = next((p for p in payloads if p["body"] == review.get("body")), None)
        review_id = review.get("id")
        if (expected is None or review.get("commit_id") != result.head or review.get("state") not in states
                or (review.get("user") or {}).get("login", "").casefold() != publisher.casefold()
                or type(review_id) is not int or review_id <= 0):
            hint = ("; for a publication format mismatch, use the publishing CLI revision or upgrade the recovery CLI"
                    if expected is None else "")
            raise CliError(f"conflicting or ambiguous review publication{hint}")
        comments = _rest_rows(f"repos/{config.repo}/pulls/{pr.number}/reviews/{review_id}/comments", "review comments")
        if (any(c.get("start_line") is not None or c.get("start_side") is not None
                or c.get("commit_id") != result.head or c.get("pull_request_review_id") != review_id
                or (c.get("user") or {}).get("login", "").casefold() != publisher.casefold()
                for c in comments)
                or sorted((c.get("path"), c.get("body")) for c in comments)
                != sorted((c.get("path"), c.get("body")) for c in expected["comments"])
                or any((c.get("line") is not None or c.get("side") is not None)
                       and not any((c.get("path"), c.get("body")) == (s.get("path"), s.get("body"))
                                   and all(c.get(k) is None or c.get(k) == s.get(k) for k in ("line", "side"))
                                   for s in expected["comments"]) for c in comments)):
            raise CliError("review finding contents or locations differ from saved publication")
    if _pr_reviews(config.repo, pr.number) != reviews:
        raise CliError("review evidence changed during publication recovery")
    fresh = github.pr_view(config.repo, pr.number)
    if (fresh.head_sha != result.head or fresh.state != "OPEN" or fresh.base_ref != pr.base_ref
            or worktree.head(wt) != result.head or worktree.is_dirty(wt)):
        raise CliError("head or worktree changed during publication recovery")
    return True


def _delta_merge_recovery(wt: Path, reviewed: str, merge: str) -> str:
    pre_merge = _git(wt, "rev-parse", f"{merge}^1")
    if not _is_ancestor(wt, reviewed, pre_merge):
        return f"replay the fixes on the reviewed head {reviewed[:12]}"
    return (f"save the current tip on a backup branch, use `git reset --hard {pre_merge}`, "
            "and cherry-pick any later fixes from the backup")


def _run_review(
    wt: Path, config: Config, head: str, kind: str, reviewed: str | None,
    model: str | None = None, effort: str | None = None, *, runtime_source: str = "",
    publication: tuple[str, int, str] | None = None,
) -> ReviewResult:
    lineage_base = _primary_base(wt, config)
    since = reviewed if kind == "delta" else lineage_base
    if kind == "delta" and not _is_ancestor(wt, since, head):
        raise CliError(
            f"reviewed head {since[:12]} is not an ancestor of this branch. Find the original "
            "branch tip with `git reflog`. Save the current tip on a backup branch, then use "
            "`git reset --hard <original-tip>` and replay any later fixes. To publish the restored "
            f"head, run `loopzero pr`, then `loopzero review` before merging {config.base_branch}, or open a new PR "
            "from the current branch"
        )
    if kind == "delta" and (merges := worktree.merges_since(wt, since)):
        recovery = _delta_merge_recovery(wt, since, merges[0])
        raise CliError(
            f"merge commit {merges[0][:12]} since the reviewed head {since[:12]} would put the "
            f"merged-in changes in the delta material. To recover, {recovery}. "
            f"To publish the recovered head, run `loopzero pr`, then `loopzero review` "
            f"before merging {config.base_branch} again"
        )
    diff = worktree.diff_since(wt, since)
    task = worktree.task_text(wt)
    authors = runners.author_families(wt, head, lineage_base)
    candidates = [family for family in config.reviewers if family not in authors]
    failures: list[str] = []
    excluded = [family for family in config.reviewers if family in authors]
    def select(family: str) -> Callable[..., ReviewResult]:
        settings = config.review.get(family)
        chosen = effort or (settings.effort if settings else None)
        if settings and chosen and settings.allowed_efforts and chosen not in settings.allowed_efforts:
            raise CliError(
                f"effort {chosen!r} is not allowed for {family}; "
                f"expected one of {settings.allowed_efforts}"
            )
        return partial(
            runners.review_with, family, cwd=wt, head=head, kind=kind, task_text=task,
            reviewer_ro_paths=config.reviewer_ro_paths,
            model=model or (settings.model if settings else None), effort=chosen,
            review_context=({"repo": config.repo, "pr": publication[1], "publisher": publication[2],
                             "head": head, "kind": kind, "base": since,
                             "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                             "diff_bytes": len(diff.encode())} if publication and family == "codex" else None),
        )
    selected = {family: select(family) for family in candidates}
    # Settle fallback settings before any launch; an unusable one only matters if reached.
    unusable: dict[str, str] = {}
    for family in excluded:
        try:
            selected[family] = select(family)
        except CliError as exc:
            unusable[family] = f"same-family fallback {family} not possible: {exc}"
    if runtime_source:
        print(runtime_source)
    fallback = ""
    reasons: list[str] = []  # published; runner detail stays in local stderr and failures
    attempts = candidates + excluded
    for index, family in enumerate(attempts):
        if family in unusable:
            failures.append(unusable[family])
            continue
        if family in excluded and not fallback:
            fallback = ("independent reviewers failed: " + "; ".join(reasons)
                        if candidates else "every configured family contributed (" + ", ".join(excluded) + ")")
            print(f"same-family fallback: {fallback}", file=sys.stderr)
        try:
            try:
                result = selected[family](diff=diff)
                break
            except runners.RunnerPromptTooLong:
                chunks = runners.split_diff(diff, config.review_chunk_bytes)
                print(
                    f"reviewer {family} rejected the full diff; reviewing {len(chunks)} chunks",
                    file=sys.stderr,
                )
                chunk_results = [selected[family](diff=chunk, chunk=(index, len(chunks)))
                                 for index, chunk in enumerate(chunks, 1)]
                result = runners.merge_reviews(chunk_results)
                break
        except RUNNER_FAILURES as exc:
            reason, detail, fallback_reason = _review_failure(family, exc)
            failures.append(detail)
            reasons.append(f"{family}: {fallback_reason}")
            if any(next_family in selected for next_family in attempts[index + 1:]):
                print(f"reviewer {family} unavailable: {reason}", file=sys.stderr)
    else:
        raise IndependentReviewerUnavailable(
            _independence_message(authors, excluded, failures)
        )
    return dataclasses.replace(result, fallback=fallback)


def _review_failure(family: str, exc: Exception) -> tuple[str, str, str]:
    lines = str(exc).splitlines()
    detail = "\n".join(lines[:3])
    fallback_reason = _FALLBACK_REASONS[3]
    if isinstance(exc, runners.RunnerAuthFailed):
        login = "claude auth login" if family == "claude" else "codex login"
        detail += f"\nFix: run `{login}`."
        fallback_reason = _FALLBACK_REASONS[0]
    elif isinstance(exc, runners.RunnerUsageLimit):
        detail += "\nFix: wait for the limit to reset or switch this reviewer's account."
        fallback_reason = _FALLBACK_REASONS[1]
    elif isinstance(exc, runners.RunnerMissing):
        package = "@anthropic-ai/claude-code" if family == "claude" else "@openai/codex"
        detail += f"\nFix: install it with `npm install -g {package}`."
        fallback_reason = _FALLBACK_REASONS[2]
    return lines[0], detail, fallback_reason


def _independence_message(
    authors: set[str], excluded: list[str], failures: list[str]
) -> str:
    identity = ", ".join(sorted(authors)) or "unknown"
    excluded_text = ", ".join(excluded) if excluded else "none"
    detail = ("\n\nFailures:\n" + "\n\n".join(failures)) if failures else ""
    return (
        f"no configured reviewer can run (author family: {identity}; excluded families: "
        f"{excluded_text}). Fix the reviewer failures below and rerun; wait for the owner "
        f"if none can run. Do not remove truthful Co-authored-by attribution.{detail}"
    )


def _review_file(wt: Path, head: str, kind: str) -> Path:
    return wt / ".loopzero" / f"review-{head[:12]}-{kind}.json"


def _save_review(wt: Path, result: ReviewResult, publication: dict | None = None) -> None:
    path = _review_file(wt, result.head, result.kind)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    data = dataclasses.asdict(result)
    if publication is not None:
        data["publication"] = publication
    if path.is_symlink():
        raise OSError(f"refusing to replace symbolic link: {path}")
    _write_json_atomically(path, data)


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
        data.pop("publication", None)
        if data.get("kind") != kind:
            raise ValueError("saved review kind mismatch")
        findings = tuple(Finding(**f) for f in data["findings"])
        # Ordinary receipts written before pair retirement still carry these defaults.
        for field, default in (("members", []), ("reconciliation", ""),
                               ("pair_authority", None), ("pair_prompt_sha256", None)):
            if data.pop(field, default) != default:
                raise ValueError("retired paired review")
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
    return pr, _readiness(wt, config, pr, head, workflow_wait=workflow_wait,
                          announce_receipt=True)


CLEANUP_RETRY = "; fix the cause, then rerun `loopzero merge` in this worktree to finish cleanup"


def _cleanup(wt: Path, pr: github.PR) -> None:
    name = None
    try:
        _repo_root(wt)
        if worktree.head(wt) != pr.head_sha:
            raise worktree.WorktreeError("local HEAD differs from merged PR head")
        name = worktree.branch(wt)
        worktree.cleanup(wt)
    except (worktree.WorktreeError, OSError) as exc:
        retry = CLEANUP_RETRY if wt.exists() else ""
        if not retry and name is not None:
            retry = f"; worktree removed; delete local branch {name!r} if it still points at the merged head"
        print(f"warning: merged but worktree cleanup failed: {exc}{retry}", file=sys.stderr)


def cmd_ready(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    pr, readiness = _pr_readiness(wt, config, workflow_wait=args.wait is not None)
    assert readiness is not None
    waiting = _draft_checks_waiting(config, pr, readiness)
    kicked = bool(waiting)
    started = time.monotonic()
    if waiting:
        github.mark_ready(config.repo, pr.number)
        if args.wait is None:
            pending = f"; {MERGEABILITY_PENDING}" if pr.mergeable == "UNKNOWN" else ""
            print(f"marked ready; waiting for required checks: {', '.join(waiting)}{pending}; {NEXT_WAIT}")
            return 3
        print("marked ready; " + "; ".join(readiness.reasons))
    if args.wait is not None and (
        waiting or _pending_checks(config, pr, readiness, allow_skipped=not pr.is_draft)
    ):
        waited = _wait_with_deadline(wt, config, pr, args.wait, kicked=kicked, started=started,
                                     shown=readiness.reasons if kicked else (), args=args,
                                     allow_skipped=True, initial=None if kicked else readiness)
        if waited is None:
            return 3
        pr, readiness, kicked = waited
    assert readiness is not None
    if readiness.ready and pr.is_draft and not kicked:
        github.mark_ready(config.repo, pr.number)
        if args.wait is not None:
            waited = _wait_with_deadline(wt, config, pr, args.wait, kicked=True, started=started,
                                         args=args, allow_skipped=True)
            if waited is None:
                return 3
            pr, readiness, kicked = waited
    assert readiness is not None
    for reason in readiness.reasons:
        print(f"not ready: {reason}")
    if not readiness.ready:
        if args.wait is None and _pending_checks(config, pr, readiness):
            print(NEXT_WAIT)
        return 1
    print(f"ready: {pr.url}")
    return 0


def _draft_checks_waiting(
    config: Config, pr: github.PR, readiness: github.Readiness
) -> tuple[str, ...]:
    reasons = set(readiness.reasons) - {MERGEABILITY_PENDING}
    if not pr.is_draft or not reasons:
        return ()
    expected: dict[str, str] = {}
    for name in config.required_ci:
        expected[f"required check '{name}' missing on {pr.head_sha[:12]}"] = name
        expected[f"required check '{name}' is skipped"] = name
    if not reasons <= expected.keys():
        return ()
    blocked = {expected[reason] for reason in reasons}
    return tuple(name for name in config.required_ci if name in blocked)


def _pending_checks(
    config: Config, pr: github.PR, readiness: github.Readiness, *, allow_skipped: bool = False
) -> tuple[str, ...]:
    """Reasons worth waiting on: mergeability or required checks can still settle.

    An explicit ready wait can observe a skipped result left over from draft CI.
    """
    states = [f"missing on {pr.head_sha[:12]}", "is pending", *(["is skipped"] if allow_skipped else [])]
    waitable = {f"required check '{n}' {state}" for n in config.required_ci for state in states}
    waitable.update(readiness.waitable_failures)
    waitable.add(MERGEABILITY_PENDING)
    reasons = readiness.reasons
    return reasons if reasons and all(r in waitable for r in reasons) else ()


def _merge_check_hint(config: Config, pr: github.PR, readiness: github.Readiness) -> str:
    if _pending_checks(config, pr, readiness):
        return "; next: loopzero merge --wait"
    if _draft_checks_waiting(config, pr, readiness):
        return "; next: loopzero ready --wait"
    return ""


def _poll_until(started: float, timeout: float, *, bounded: bool = False) -> Iterator[None]:
    delay = WAIT_INTERVAL
    while True:
        yield  # Observe immediately, including --wait=0.
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return
        pause = delay * random.uniform(0.9, 1.0)
        if bounded and remaining <= pause:
            # Leave part of the final interval for the last observation to complete.
            _sleep(remaining / 2)
            yield
            remaining = timeout - (time.monotonic() - started)
            if remaining > 0:
                _sleep(remaining)
            return
        _sleep(min(remaining, pause))
        delay = min(120.0, delay * 2)


def _wait_with_deadline(
    wt: Path, config: Config, pr: github.PR, timeout: float, *, kicked: bool,
    started: float | None = None, shown: tuple[str, ...] = (), args: argparse.Namespace,
    allow_merged: bool = False, allow_skipped: bool = False, initial: github.Readiness | None = None,
) -> tuple[github.PR, github.Readiness | None, bool] | None:
    started = time.monotonic() if started is None else started
    if timeout == 0:
        return _wait_for_checks(wt, config, pr, timeout, kicked=kicked, started=started,
                                shown=shown, args=args, allow_merged=allow_merged,
                                allow_skipped=allow_skipped, initial=initial)
    deadline = started + timeout
    if time.monotonic() >= deadline:
        _print_check_wait_timeout(pr)
        return None
    reported_timeout = False
    try:
        with github.bounded_reads(deadline, token=None, retry=True):
            waited = _wait_for_checks(wt, config, pr, timeout, kicked=kicked, started=started,
                                      shown=shown, args=args, bounded=True, allow_merged=allow_merged,
                                      allow_skipped=allow_skipped, initial=initial)
            reported_timeout = waited is None
    except github.ReadDeadline:
        if not reported_timeout:
            _print_check_wait_timeout(pr)
        return None
    return waited


def _print_check_wait_timeout(pr: github.PR) -> None:
    if pr.mergeable == "UNKNOWN":
        print(f"timed out waiting for GitHub to compute mergeability on {pr.head_sha[:12]}")
    else:
        print(f"timed out waiting for required checks on {pr.head_sha[:12]}")


def _verify_wait_snapshot(
    args: argparse.Namespace | None, wt: Path, config: Config, head: str, current_head: str,
) -> None:
    if current_head != head:
        raise CliError(f"PR head changed while waiting: {head[:12]} to {current_head[:12]}")
    if args is None:
        return
    latest = _load_config(args, wt, pin_checks=True)
    changed = "workflow settings"
    if latest == config:
        if (latest.trusted_base_revision == config.trusted_base_revision
                or not (config.check_runner or config.inputs)):
            return
        changed = "check base"
    if _current_check_report(wt, latest, head) is None:
        raise CliError(f"{changed} changed while waiting; run loopzero check and retry")
    raise CliError(f"{changed} changed while waiting; retry with the current settings")


def _wait_for_checks(
    wt: Path, config: Config, pr: github.PR, timeout: float, *, kicked: bool,
    started: float | None = None, shown: tuple[str, ...] = (),
    args: argparse.Namespace | None = None, bounded: bool = False, allow_merged: bool = False,
    allow_skipped: bool = False, initial: github.Readiness | None = None,
) -> tuple[github.PR, github.Readiness | None, bool] | None:
    """Poll the same head until it settles, omitting readiness for an allowed merge."""
    grace_started = time.monotonic()
    started = grace_started if started is None else started
    current = pr
    stale_notice = False
    for _ in _poll_until(started, timeout, bounded=bounded):
        if initial is not None:
            readiness = initial
            initial = None
        else:
            current = github.pr_view(config.repo, pr.number)
            _verify_wait_snapshot(args, wt, config, pr.head_sha, current.head_sha)
            if allow_merged and current.state == "MERGED":
                return current, None, kicked
            readiness = _readiness(wt, config, current, pr.head_sha, workflow_wait=True)
        if not kicked and _draft_checks_waiting(config, current, readiness):
            github.mark_ready(config.repo, pr.number)
            print("marked ready; " + "; ".join(readiness.reasons))
            return _wait_for_checks(wt, config, pr, timeout, kicked=True, started=started,
                                    shown=readiness.reasons, args=args, bounded=bounded,
                                    allow_merged=allow_merged)
        if kicked and readiness.ready and timeout > 0 and time.monotonic() - started > timeout:
            break  # A post-transition read cannot grant success beyond the original deadline.
        pending = _pending_checks(
            config, current, readiness, allow_skipped=kicked or (allow_skipped and not current.is_draft),
        )
        if not pending:
            return current, readiness, kicked
        if pending != shown:
            print("waiting: " + "; ".join(pending))
            shown = pending
        elapsed = time.monotonic() - grace_started
        if readiness.stale_verdicts and not stale_notice:
            print(
                "waiting: required check verdict predates the review; awaiting refresh: "
                + ", ".join(readiness.stale_verdicts)
            )
            stale_notice = True
        # Missing check runs do not prove that no workflow was scheduled. Diagnose only
        # when no visible pending check explains why the required jobs have not appeared.
        if (
            elapsed >= MISSING_RUN_GRACE
            and all(" missing on " in r for r in pending)
            and not readiness.has_pending_checks
        ):
            raise CliError(
                f"{'; '.join(pending)} after {elapsed:.0f}s: inspect workflow scheduling, "
                f"triggers and permissions for head {pr.head_sha[:12]}; after correcting the cause, "
                "rerun the same `--wait` command"
            )
    _print_check_wait_timeout(current)
    return None


def _landed(config: Config, pr: github.PR, closed: list[github.GhError]) -> str | None:
    """Merge SHA if landed; a closed-unmerged read is held as not final (GitHub lags a
    queue merge) and re-raised by the caller only if it persists to the deadline."""
    try:
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
    except github.ClosedUnmerged as exc:
        closed[:] = [exc]
        return None
    closed.clear()
    return sha


def _wait_for_merge(config: Config, pr: github.PR, timeout: float) -> str | None:
    """Poll until the queue lands the PR and return the merge SHA; None on timeout."""
    started = time.monotonic()
    closed: list[github.GhError] = []
    print(f"PR #{pr.number} is queued for merge into {config.base_branch}; waiting")
    for _ in _poll_until(started, timeout):
        sha = _landed(config, pr, closed)
        if sha:
            return sha
        if not github.merge_pending(config.repo, pr.number):
            sha = _landed(config, pr, closed)
            if sha:
                return sha
            if closed:
                continue
            raise CliError(f"PR #{pr.number} left the merge queue unmerged")
    if closed:
        raise closed[0]
    return None


def _mergify_timeout_detail(
    config: Config, pr: github.PR, queue: str, state: mergify.Membership | None,
) -> str:
    if state is None:
        try:
            _, _, report = mergify.request_status(config.repo, pr.number, pr.head_sha, queue)
        except github.GhError:
            report = None
        return f"waiting for admission to Mergify queue {queue}. " + (
            f"Mergify report: {report}" if report else "Check the PR for Mergify's admission report."
        )
    location = (
        f"https://github.com/{config.repo}/pull/{state.candidate_number}"
        if state.candidate_number is not None else
        f"https://dashboard.mergify.com/orgs/{config.repo.replace('/', '/repos/', 1)}/queues/status"
        f"?branch={config.base_branch}&pull={pr.number}"
    )
    detail = f"still queued in {queue}. {location}"
    if state.unmet_conditions:
        detail += ". Unmet candidate conditions: " + "; ".join(state.unmet_conditions)
    return detail


MERGIFY_DEQUEUE_HINT = (
    'Read Mergify\'s "Left the queue" comment on the PR for the reason. '
    "Address it before requeueing. To requeue, post `@mergifyio queue` as the whole PR comment "
    "(same login), then rerun `loopzero merge --wait`."
)


def _mergify_departure(
    config: Config, pr: github.PR, queue: str, closed: list[github.GhError],
) -> str | None:
    sha = _landed(config, pr, closed)
    if not sha and not closed:
        raise CliError(f"PR #{pr.number} left Mergify queue {queue} unmerged. {MERGIFY_DEQUEUE_HINT}")
    return sha


def _wait_for_mergify(
    config: Config, pr: github.PR, timeout: float, confirmed: bool
) -> str | None:
    """Follow one exact source head from request through confirmed membership to merge."""
    started = time.monotonic()
    closed: list[github.GhError] = []
    queue = config.mergify_queue
    assert queue is not None
    state = None
    deferred_landing_read = False
    print(f"PR #{pr.number} following Mergify queue {queue}; head {pr.head_sha[:12]}; waiting up to {timeout:g}s")
    for _ in _poll_until(started, timeout):
        sha = _landed(config, pr, closed)
        if sha:
            return sha
        if not mergify.configured(config.repo, config.base_branch, queue):
            raise CliError("Mergify queue configuration became unsafe or unavailable while waiting")
        state = mergify.membership(config.repo, pr.number, queue)
        deferred_landing_read = confirmed and state is not None and not closed
        if not deferred_landing_read and (sha := _landed(config, pr, closed)):
            return sha
        if state is not None and not confirmed:
            mergify.mark_confirmed(config.repo, pr.number, pr.head_sha, queue)
            confirmed = True
            print(f"PR #{pr.number} confirmed in Mergify queue {queue}; waiting")
        elif state is None and confirmed:
            if sha := _mergify_departure(config, pr, queue, closed):
                return sha
    # A queued, open PR normally gets its next landing read on the next tick.
    if deferred_landing_read and (sha := _landed(config, pr, closed)):
        return sha
    if closed:
        raise closed[0]
    print(f"timed out waiting for Mergify to land PR #{pr.number}")
    print(_mergify_timeout_detail(config, pr, queue, state))
    return None


class _AdmissionDeadline(Exception):
    """The --wait budget expired before a new merge or queue request."""


def _merge_with_mergify(
    config: Config, pr: github.PR,
    readiness: github.Readiness, deadline: float | None, admit_by: float | None = None,
) -> str | None:
    """Request Mergify after readiness verifies the source gates and queue configuration."""
    if not readiness.ready:
        hint = _merge_check_hint(config, pr, readiness)
        raise CliError("not ready to request Mergify: " + "; ".join(readiness.reasons) + hint)
    queue = config.mergify_queue
    assert queue is not None
    requested, confirmed_marker = mergify.markers(
        config.repo, pr.number, pr.head_sha, queue
    )
    state = mergify.membership(config.repo, pr.number, queue)
    if sha := github.merged_sha(
        config.repo, pr.number, expected_head=pr.head_sha, check_hold=not requested and state is None,
    ):
        return sha
    confirmed = state is not None
    if confirmed and not confirmed_marker:
        mergify.mark_confirmed(config.repo, pr.number, pr.head_sha, queue)
        confirmed_marker = True
    if not confirmed and confirmed_marker:
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
        if sha:
            return sha
        raise CliError(f"PR #{pr.number} left Mergify queue {queue} unmerged. {MERGIFY_DEQUEUE_HINT}")
    if not confirmed and not requested:
        if admit_by is not None and time.monotonic() > admit_by:
            raise _AdmissionDeadline
        mergify.request(config.repo, pr.number, pr.head_sha, queue)
        requested = True
    if deadline is not None:
        return _wait_for_mergify(config, pr, max(0.0, deadline - time.monotonic()), confirmed)
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
    pr, readiness = _pr_readiness(
        wt, config, allow_merged=True, workflow_wait=args.wait is not None
    )
    started = time.monotonic()
    deadline = None if args.wait is None else started + args.wait
    # --wait=0 observes once; a positive budget also bounds merge or queue admission.
    admit_by = deadline if args.wait else None
    if readiness is not None and args.wait is not None and _pending_checks(config, pr, readiness):
        # One waiter and one deadline span source checks and landing: a check created
        # by the ready transition must not end the turn between `ready` and the queue.
        waited = _wait_with_deadline(wt, config, pr, args.wait, kicked=False, started=started,
                                     args=args, allow_merged=True, initial=readiness)
        if waited is None:
            return 3
        pr, readiness, _ = waited
    if readiness is None:
        sha = github.merged_sha(
            config.repo, pr.number,
            expected_head=pr.head_sha,
        )
        if sha is None:
            raise CliError(f"PR #{pr.number} reports MERGED but has no merge commit yet; rerun")
        print(f"PR #{pr.number} was already merged as {sha[:12]}; cleaning up")
    elif config.merge_strategy == "mergify":
        try:
            sha = _merge_with_mergify(config, pr, readiness, deadline, admit_by)
        except _AdmissionDeadline:
            print(f"timed out before requesting Mergify for PR #{pr.number}")
            return 3
        if sha is None:
            return 3 if args.wait is not None else 0
    else:
        if not readiness.ready:
            hint = _merge_check_hint(config, pr, readiness)
            raise CliError("not ready to merge: " + "; ".join(readiness.reasons) + hint)
        if admit_by is not None and time.monotonic() > admit_by:
            print(f"timed out before merging PR #{pr.number}")
            return 3
        sha = github.merge(config.repo, pr.number, config.merge_strategy, pr.head_sha)
        if sha is None and args.wait is not None:
            sha = _wait_for_merge(config, pr, max(0.0, deadline - time.monotonic()))
            if sha is None:
                print(f"timed out waiting for the merge queue to land PR #{pr.number}")
                return 3
        if sha is None:
            print(
                f"PR #{pr.number} is queued for merge into {config.base_branch}; "
                "next: loopzero merge --wait follows the queue, verifies and cleans up"
            )
            return 0
    _cleanup_remote_refs(wt, config, branch, pr.head_sha)
    _cleanup(wt, pr)
    print(sha)
    return 0


def _cleanup_remote_refs(wt: Path, config: Config, branch: str, expected_head: str) -> None:
    try:
        if github.delete_remote_branch(config.repo, branch, expected_head):
            _delete_tracking_ref(wt, branch, expected_head)
            return
        print(f"warning: merged; remote branch {branch} moved from {expected_head[:12]}; "
              "skipping deletion", file=sys.stderr)
    except (github.GhError, _proc.ProcTimeout) as exc:
        print(f"warning: merged but remote branch cleanup failed for {branch}: {exc}; "
              f"delete {branch!r} in {config.repo} if it still points at the merged head", file=sys.stderr)


def _delete_tracking_ref(wt: Path, branch: str, expected_head: str) -> None:
    if not branch.startswith("lz/") or branch.count("/") != 1:
        return
    ref = f"refs/remotes/origin/{branch}"
    try:
        _repo_root(wt)
        current = _git(wt, "for-each-ref", "--format=%(objectname) %(refname) %(symref)", ref)
        if current == f"{expected_head} {ref}":
            _git(wt, "update-ref", "--no-deref", "-d", ref, expected_head)
    except (CliError, OSError, _proc.ProcTimeout) as exc:
        print(f"warning: merged but tracking ref cleanup failed for {ref}: {exc}", file=sys.stderr)


def cmd_resolve(args: argparse.Namespace) -> int:
    """List open blocking findings, or reply to one with what changed and resolve its thread."""
    wt, config = _context(args)
    pr = _require_pr(config, worktree.branch(wt))
    if not args.finding:
        for marker_id, _thread, f in github.open_findings(config.repo, pr.number):
            print(f"{marker_id}  {f.severity}  {f.path or '-'}:{f.line or '-'}  {f.title}")
        return 0
    finding = github.resolve_finding(config.repo, pr.number, args.finding, args.reply)
    print(f"resolved {args.finding}: {finding.title}")
    try:
        with github.api_error_diagnostics():
            remaining = github.open_blocking_findings(config.repo, pr.number)
    except (github.GhError, _proc.ProcTimeout, OSError) as exc:
        retry = shlex.join(("gh", *github.eligibility_dispatch_args(
            config.repo, pr.number, config.base_branch,
        )))
        raise CliError(
            f"Resolved finding {args.finding} on PR #{pr.number}, but could not refresh "
            f"Loop-zero Eligibility. {exc}\n"
            "To check remaining findings, run `loopzero resolve` with no arguments. "
            f"Then, if no findings remain, run `{retry}`."
        ) from exc
    if not remaining:
        github.refresh_eligibility(config.repo, pr.number, config.base_branch)
        print(f"refreshed Loop-zero Eligibility for PR #{pr.number}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    wt = _toplevel()
    config = _load_config(args, wt, pin_checks=True)
    branch, head = worktree.branch(wt), worktree.head(wt)
    print(f"worktree: {wt}")
    print(f"branch:   {branch}")
    print(f"head:     {head}")
    print(f"base:     {config.base_branch}")
    print(f"dirty:    {'yes' if worktree.is_dirty(wt) else 'no'}")
    pr = github.pr_for_branch(config.repo, branch)
    if pr is None:
        print("pr:       none (commit changes and pass `loopzero check` before `loopzero pr`)")
        return 0
    print(f"pr:       #{pr.number} {pr.url} ({'draft' if pr.is_draft else 'ready'}, {pr.state})")
    reviews = _pr_reviews(config.repo, pr.number)
    markers = _pr_markers(reviews, head, config.review_publishers, repo=config.repo, number=pr.number)
    if markers:
        latest = markers[-1]
        print(f"review:   {latest.kind} on {latest.head[:12]} ({latest.state.lower() or 'posted'})")
    else:
        print("review:   none on this PR")
    readiness = _readiness(wt, config, pr, head, reviews)
    print(f"ready:    {'yes' if readiness.ready else 'no'}")
    for reason in readiness.reasons:
        print(f"  - {reason}")
    if config.merge_strategy == "mergify" and pr.state == "OPEN" and pr.head_sha == head:
        _print_mergify_status(config, pr, readiness)
    return 0


def _print_mergify_status(config: Config, pr: github.PR, readiness: github.Readiness) -> None:
    queue = config.mergify_queue
    assert queue is not None
    requested, confirmed, report = mergify.request_status(config.repo, pr.number, pr.head_sha, queue)
    state = mergify.membership(config.repo, pr.number, queue)
    if state:
        label = "confirmed"
    elif confirmed:
        label = "ejected"
    elif requested or (readiness.ready and report):
        label = "waiting for admission"
    else:
        label = "not admitted"
    print(f"mergify:  {label} ({queue})")
    if label == "waiting for admission" and report:
        print(f"  Mergify report: {report}")


# --------------------------------------------------------------------------- entry point


def _wait_seconds(value: str) -> float:
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected a finite, nonnegative number of seconds")


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
    review = sub.add_parser("review", help="run native review and post one canonical review")
    review.add_argument(
        "--repost", action="store_true", help="post the saved review for HEAD without a model"
    )
    review.add_argument("--model", help="reviewer model override for a fresh review")
    review.add_argument("--effort", help="reviewer effort override for a fresh review")
    review.set_defaults(func=cmd_review)
    ready = sub.add_parser(
        "ready", help="compute readiness and mark the PR ready; --wait polls required checks"
    )
    merge = sub.add_parser(
        "merge", help="recheck readiness, merge and clean up; --wait waits for checks and landing"
    )
    for waiter, what in ((ready, "required checks on this head"), (merge, "required checks and the landed merge")):
        waiter.add_argument(
            "--wait", nargs="?", const=WAIT_TIMEOUT, type=_wait_seconds, metavar="SECONDS",
            help=f"wait for {what} (default {WAIT_TIMEOUT:.0f}s); exit 3 on timeout",
        )
    merge.add_argument(
        "--cancel", action="store_true", help="request removal from the configured Mergify queue"
    )
    ready.set_defaults(func=cmd_ready)
    merge.set_defaults(func=cmd_merge)
    resolve = sub.add_parser(
        "resolve", usage='%(prog)s [<finding-id> "<reply>"]',
        help="list open findings, or reply to one and resolve it",
    )
    resolve.add_argument("finding", nargs="?", help="finding id from the list (8 hex digits)")
    resolve.add_argument("reply", nargs="?", default="", help="what changed, e.g. the fix commit")
    resolve.set_defaults(func=cmd_resolve)
    status = sub.add_parser("status", help="show where this branch is in the sequence")
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
    # Progress lines must precede the stderr verdict when both go to one file.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "resolve" and args.finding is not None and not args.reply.strip():
        parser.error('expected resolve [<finding-id> "<reply>"] with a nonblank reply for a finding')
    if args.command == "review" and args.repost:
        conflicts = [f"--{name}" for name in ("model", "effort") if getattr(args, name) is not None]
        if conflicts:
            parser.error(f"review --repost cannot be combined with {' or '.join(conflicts)}")
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
