"""Command line: composes the modules into start, check, pr, review, ready, merge, status."""

from __future__ import annotations

from loopzero._cli_args import build_parser, parse_args

# Module execution parses help and version before loading delivery code.
if __name__ == "__main__":
    _module_args = parse_args(build_parser(), None)

import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
import random
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
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
# --wait: poll interval, default timeout, and how long a required check may stay
# missing before GitHub is assumed to have dropped the event for this head.
# Each poll costs several GitHub API calls from a quota shared by every session.
WAIT_INTERVAL, MISSING_RUN_GRACE = 30.0, 120.0
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


def _git(cwd: Path, *args: str, timeout: float = GIT_TIMEOUT, paths: bool = False,
         extra_env: dict[str, str] | None = None) -> str:
    run = _proc.run_git_paths if paths else _proc.run
    done = run(["git", *args], cwd=cwd, env_allowlist=worktree.GIT_ENV, timeout=timeout, extra_env=extra_env)
    if done.exit_code != 0:
        raise CliError(f"git {' '.join(args)} failed: {_proc.tail(done.stderr, 1)}")
    return done.stdout if paths else done.stdout.strip()


def _require_push_target(wt: Path, repo: str) -> None:
    urls = _git(wt, "remote", "get-url", "--push", "--all", "origin").splitlines()
    github_url = re.compile(
        r"(?:https://(?:[a-z0-9._~%:+-]+@)?github\.com(?::443)?/"
        r"|git@github\.com:|ssh://git@github\.com(?::22)?/|ssh://git@ssh\.github\.com:443/)"
        + re.escape(repo) + r"(?:\.git)?/?",
        re.IGNORECASE | re.ASCII,
    )
    if not urls or any(not github_url.fullmatch(url) for url in urls):
        raise CliError(f"origin push URL does not target {repo}; correct the Git remote before running pr")


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
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel", paths=True))


def _repo_root(wt: Path) -> Path:
    """The verified primary checkout or bare repository owning this linked worktree."""
    first = _git(wt, "worktree", "list", "--porcelain", "-z", paths=True).split("\0", 1)[0]
    if not first.startswith("worktree "):
        raise CliError("Git did not identify the primary worktree; refusing cleanup")
    root = Path(first.removeprefix("worktree "))
    common = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", paths=True)).resolve()
    if root.resolve() == wt.resolve() or common not in (root.resolve(), (root / ".git").resolve()):
        raise CliError("primary repository does not own this linked worktree; refusing cleanup")
    return root


def _load_config(
    args: argparse.Namespace, root: Path, *, pin_checks: bool = False, previous: Config | None = None,
    refresh: bool = False,
) -> Config:
    path = args.config or root / "workflow.toml"
    if not pin_checks:
        return config_mod.load(path)
    if args.config:
        print("checks are unpinned because --config was provided")
        return config_mod.load(path)
    config = config_mod.load_trusted(root, previous=previous, refresh=refresh)
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
    return wt, _load_config(
        args, wt, pin_checks=pin_checks, refresh=args.command in {"ready", "merge"},
    )


def _is_ancestor(wt: Path, sha: str, head: str) -> bool:
    done = config_mod.run_git(wt, "merge-base", "--is-ancestor", sha, head)
    if done.exit_code not in (0, 1):
        detail = _proc.tail(done.stderr or done.stdout, 1)
        raise CliError(f"git merge-base --is-ancestor failed (exit {done.exit_code}): "
                       f"{detail or 'retry the ancestry check'}")
    return done.exit_code == 0


def _primary_base(wt: Path, config: Config) -> str:
    base = config.trusted_base_revision or config_mod.base_revision(wt, config.base_branch)
    done = config_mod.run_git(wt, "merge-base", base, "HEAD")
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
        raise CliError(f"branch {branch!r} is the configured base; use a task branch")


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
        batch = github.api_get(f"{endpoint}?per_page={github.PAGE}&page={page}")
        if not isinstance(batch, list) or not all(isinstance(row, dict) for row in batch):
            raise CliError(f"invalid GitHub {what} response")
        rows += batch
        if len(batch) < github.PAGE:
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
            "review cap reached: fix or reject the findings "
            "(resolve-findings) and run `loopzero ready`, or split the PR"
        )
    primary = next((m for m in markers if m.kind == "primary"), None)
    if primary is None:
        return "primary", None
    reviewed = primary.head
    if head.startswith(reviewed) or reviewed.startswith(head):
        raise CliError(f"head {head[:12]} already has a primary review; run `loopzero ready`, or push fixes for the delta")
    return "delta", reviewed


def _conflict_delta_base(config: Config, pr: github.PR, head: str, reviews: list[dict],
                         markers: list[Marker], publishers: tuple[str, ...]) -> str | None:
    rounds = list(dict.fromkeys((marker.kind, marker.head) for marker in markers))
    if [kind for kind, _ in rounds] != ["primary", "delta"]:
        return None
    (_, primary), (_, reviewed) = rounds
    if len({primary, reviewed, head}) != 3:
        return None
    publishers = publishers or (github.login(),)
    if not eligibility.approved_delta(reviews, reviewed, publishers, repo=config.repo, number=pr.number):
        return None
    if eligibility.base_merge_material(
        config.repo, pr.number, reviewed, head, config.base_branch, clean=False, conflict_only=True,
    ) is None:
        return None
    if github.open_blocking_findings(config.repo, pr.number, None, publishers):
        return None
    return reviewed


def _select_review_round(config: Config, pr: github.PR, head: str, reviews: list[dict]) -> tuple[str, str | None]:
    publishers = config.review_publishers
    markers = _pr_markers(reviews, head, publishers, repo=config.repo, number=pr.number)
    if reviewed := _conflict_delta_base(config, pr, head, reviews, markers, publishers):
        return "delta", reviewed
    return _decide_kind(markers, head)


def _review_cap_reason(config: Config, pr: github.PR, head: str, reviews: list[dict],
                       markers: list[Marker], publishers: tuple[str, ...]) -> str:
    if _conflict_delta_base(config, pr, head, reviews, markers, publishers):
        return f"Run `loopzero review` for the available conflict-resolution delta on {head[:12]}."
    return (
        f"The review budget is exhausted and {head[:12]} has no eligible review. "
        "Open a replacement PR for changes that need review. Another `loopzero review` on this PR will be refused."
    )


MERGEABILITY_PENDING = "GitHub is computing mergeability"


def _readiness(
    wt: Path, config: Config, pr: github.PR, head: str, reviews: list[dict] | None = None,
    *, workflow_wait: bool = False, announce_receipt: bool = False,
) -> github.Readiness:
    if hold := github.merge_hold(pr.number, pr.labels):
        return github.Readiness(False, (hold,))
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
        publishers=publishers,
    )
    report = _load_report(wt)
    receipt_problem = _check_report_problem(report, wt, config, head)
    if receipt_problem:
        result = github.Readiness(False, (*result.reasons, receipt_problem))
    elif announce_receipt:
        _print_receipt(wt, report, "accepted")
    if config.merge_strategy == "mergify" and pr.state == "OPEN":
        behind = github.behind_reason(pr, head if review else None)
        reasons = result.reasons
        if mergify.configured(config.repo, config.base_branch, config.mergify_queue or ""):
            reasons = tuple(reason for reason in reasons if reason != behind)
        else:
            reasons = (*reasons, "Mergify queue configuration is unverified or permits in-place head updates")
        result = dataclasses.replace(result, ready=not reasons, reasons=reasons)
    if github.MISSING_REVIEW_REASON in result.reasons:
        markers = _pr_markers(reviews, head, publishers, repo=config.repo, number=pr.number)
        if _review_cap_spent(markers) and all(marker.head != head for marker in markers):
            exhausted = _review_cap_reason(config, pr, head, reviews, markers, publishers)
            result = dataclasses.replace(result, reasons=tuple(
                exhausted if reason == github.MISSING_REVIEW_REASON else reason for reason in result.reasons
            ))
    if pr.state == "OPEN" and pr.mergeable == "UNKNOWN":
        result = dataclasses.replace(result, ready=False, reasons=(*result.reasons, MERGEABILITY_PENDING))
    return result


# --------------------------------------------------------------------------- checks report


def _check_report_problem(
    report: CheckReport | None, wt: Path, config: Config, head: str, *, require_pass: bool = True,
) -> str | None:
    if report is None:
        return "run loopzero check at this head"
    if report.head != head:
        return f"check receipt is for {report.head[:12]}. Run `loopzero check` at {head[:12]}."
    if report.dirty:
        return ("check receipt was recorded in a dirty worktree. Run `git status --short`, "
                "settle the changes, then run `loopzero check`.")
    commands = tuple(result.command for result in report.results)
    if (require_pass and not report.ok) or commands != config.checks:
        if not commands or commands[-1] == sandbox.INCOMPLETE_CHECK_COMMAND:
            return "check receipt is incomplete. Run `loopzero check` to resume with the current workflow."
        if require_pass and not report.ok:
            return ("check receipt records a failed stage. Fix the failure, then run `loopzero check`. "
                    "Retry unchanged only if the failure was transient.")
        return ("check receipt's plan differs from the trusted workflow. "
                "Run `loopzero check` with the current workflow.")
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


def _save_report(wt: Path, report: CheckReport) -> str | None:
    """Publish the receipt or return the current HEAD if the result is stale."""
    payload = {
        "head": report.head,
        "dirty": report.dirty,
        "results": [{k: v for k, v in dataclasses.asdict(result).items()
                     if k not in {"input_key", "reused_from"} or v is not None}
                    for result in report.results],
    }
    if report.settings_sha256 is not None:
        payload["settings_sha256"] = report.settings_sha256
    return worktree.write_local_state(
        wt, CHECKS_FILE.name, json.dumps(payload, indent=2) + "\n", expected_head=report.head,
    )


def _load_report(wt: Path) -> CheckReport | None:
    path = wt / CHECKS_FILE
    try:
        data = json.loads(worktree.read_local_state(wt, path.name))
        results = tuple(CheckResult(**item) for item in data["results"])
        if any(not is_check_exit_code(result.exit_code) for result in results):
            raise TypeError("exit_code must be an integer")
        dirty = data.get("dirty", True)
        if not isinstance(dirty, bool):
            raise TypeError("dirty must be a bool")
        return CheckReport(head=str(data["head"]), dirty=dirty, results=results,
                           settings_sha256=data.get("settings_sha256"))
    except FileNotFoundError:
        return None
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
    lines += [f"- `{github.redact_credentials(r.command)}`: exit {r.exit_code}"
              for r in results] or ["- (no check commands configured)"]
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
    try:
        with worktree.lock_local_state(wt):
            upstream = config_mod.run_git(wt, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
            if upstream.exit_code == 0:
                branch, head = worktree.branch(wt), worktree.head(wt)
                pr = github.pr_for_branch(config.repo, branch, open_only=True)
                if pr is not None and pr.head_sha == head:
                    body = _pr_body(wt, head, config, pr.body)
                    if body != (pr.body or "").replace("\r\n", "\n"):
                        github.update_body(config.repo, pr.number, body)
    except (github.GhError, _proc.ProcTimeout, OSError, ValueError, CliError, worktree.WorktreeError) as exc:
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
    current = config_mod.run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current.stdout.strip().startswith("lz/") or (
        current.exit_code == 1 and worktree.is_linked_checkout(root)
    ):
        raise CliError(f"already inside task worktree {root}; run start from the main checkout")
    config = _load_config(args, root)
    print(worktree.start(root, args.slug, config.base_branch))
    return 0


def _check_failure_preview(tail: str, limit: int = 1536) -> str:
    if len(tail.encode()) <= limit:
        return tail
    edge = limit // 8
    lines = (line.encode() for line in tail.splitlines())
    preview = b"\n".join(
        line if len(line) <= 2 * edge else line[:edge] + b" [...] " + line[-edge:]
        for line in lines
    )
    if len(preview) > limit:
        preview = b"[...]\n" + preview[6 - limit:]
    return preview.decode(errors="ignore")


def cmd_check(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    worktree.local_state_files(wt, CHECKS_FILE.name)
    if not config.checks:
        raise config_mod.ConfigError("checks.commands must list at least one command to run checks")
    try:
        report = sandbox.run_checks(config, wt, progress=lambda command: print(
            f"running: {github.redact_credentials(command)}", flush=True),
            status=lambda message: print(message, flush=True),
            checkpoint=lambda partial: _save_report(wt, partial),
            workflow=args.config or f"origin/{config.base_branch}:workflow.toml")
    except sandbox.SandboxUnavailable as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return 2
    shortened = False
    for result in report.results:
        if result.exit_code != 0:
            heading = github.redact_credentials(result.command)
            print(f"--- {heading} (exit {result.exit_code}) ---", file=sys.stderr)
            tail = _proc.tail(result.tail)
            preview = _check_failure_preview(tail)
            shortened |= preview != tail
            print(preview, file=sys.stderr)
            if not config.network and (result.command.startswith("uv sync --offline --locked")
                                       or OFFLINE_FAILURE_RE.search(tail)):
                env = sandbox.check_environment(config)
                print(
                    "\nThe sandbox has no network because `[checks] network = false`; "
                    f"the effective UV_CACHE_DIR inside the sandbox is {env['UV_CACHE_DIR']}. "
                    "Run UV_CACHE_DIR=<effective-cache> uv sync --group dev on the host "
                    "(use the required groups) so the cache under `[checks] writable` is warm, "
                    "or set `[checks] env` UV_CACHE_DIR to a warm cache.", file=sys.stderr,
                )
    if (current_head := _save_report(wt, report)) is not None:
        print(f"check result discarded: checked HEAD {report.head}, current HEAD {current_head}",
              file=sys.stderr)
        return 1
    if shortened:
        print(f"Full diagnostic tail: {CHECKS_FILE} (head {report.head})", file=sys.stderr)
    if report.ok and report.settings_sha256 is None:
        print("check stages passed but the receipt is unverified; rerun loopzero check from the task "
              "worktree with a stable, readable configured runner before running ready", file=sys.stderr)
        _refresh_pr_checks(wt, config)
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
        known = _proc.run(["git", "cat-file", "-e", f"{pr.head_sha}^{{commit}}"], cwd=wt,
                          env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT)
        if known.exit_code != 0:
            raise CliError(
                f"PR head {pr.head_sha[:12]} is not in this worktree; fetch and integrate it first"
            )
        force_lease = pr.head_sha != head and not _is_ancestor(wt, pr.head_sha, head)
        if force_lease:
            previous_heads = _git(wt, "reflog", "show", "--format=%H", f"refs/heads/{branch}").splitlines()
            if not any(_is_ancestor(wt, pr.head_sha, previous) for previous in previous_heads):
                raise CliError(
                    f"PR head {pr.head_sha[:12]} is absent from this task branch's history; integrate it first"
                )
            push.extend(("--porcelain", f"--force-with-lease={branch}:{pr.head_sha}"))
    push.append(branch)
    _require_push_target(wt, config.repo)
    _git_push(wt, push, force_lease=force_lease)
    if pr is not None and pr.state == "OPEN":
        with worktree.lock_local_state(wt):
            live = github.pr_view(config.repo, pr.number)
            if live.state != "OPEN":
                raise CliError(f"PR #{pr.number} closed during publication")
            body = _pr_body(wt, head, config, _sync_task_narrative(wt, live.body or ""))
            if body != live.body.replace("\r\n", "\n"):
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
        git_dir = _git(wt, "rev-parse", "--absolute-git-dir", paths=True)
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
    kinds = ("primary", "delta")
    _, *saved_files = worktree.local_state_files(
        wt, "task.md", *(_review_file(wt, head, kind).name for kind in kinds)
    )
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
    saved = [kind for kind, exists in zip(kinds, saved_files, strict=True) if exists] if args.repost else []
    if len(saved) > 1:
        raise CliError("multiple saved reviews for HEAD; publication is ambiguous")
    reviews = [] if saved else _pr_reviews(config.repo, pr.number)
    if saved:
        kind = saved[0]
    else:
        kind, reviewed = _select_review_round(config, pr, head, reviews)
    publication_context = {"repo": config.repo, "number": pr.number, "publisher": publisher}
    if args.repost:
        result, publication = _load_review(wt, head, kind)
        candidates, excluded = _repost_reviewer_families(wt, config, head, result)
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
        if result.fallback and not _published_fallback(result.fallback, candidates, excluded):
            raise CliError("saved fallback note is not in the published form; run without --repost")
        allowed, reviewed = _select_review_round(config, pr, head, reviews)
        if allowed != kind:
            raise CliError("saved review kind differs from the remaining review budget")
        _delta_patch(wt, config, reviewed, head, pr.number)
    else:
        if args.model is None and args.effort is None and _has_recoverable_review(
            wt, config, head, kind, publication_context,
        ):
            raise CliError(f"saved {kind} review for {head[:12]}; run `loopzero review --repost`")
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
    _save_review(wt, result, publication_context)
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
        with worktree.lock_local_state(wt):
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


def _repost_reviewer_families(
    wt: Path, config: Config, head: str, result: ReviewResult,
) -> tuple[list[str], list[str]]:
    authors = runners.author_families(wt, head, _primary_base(wt, config))
    excluded = [family for family in config.reviewers if family in authors]
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
    return [family for family in config.reviewers if family not in authors], excluded


def _has_recoverable_review(
    wt: Path, config: Config, head: str, kind: str, publication: dict,
) -> bool:
    other_kind = "delta" if kind == "primary" else "primary"
    if not _review_file(wt, head, kind).exists() or _review_file(wt, head, other_kind).exists():
        return False
    try:
        result, _ = _load_review(wt, head, kind, publication=publication)
        candidates, excluded = _repost_reviewer_families(wt, config, head, result)
        if result.fallback and not _published_fallback(result.fallback, candidates, excluded):
            return False
        evidence = runners.codex_evidence(result)
    except (CliError, runners.RunnerBadOutput):
        return False
    return evidence is None or (evidence["repo"], evidence["pr"], evidence["publisher"]) == (
        publication["repo"], publication["number"], publication["publisher"],
    )


def _repost_prefix(result: ReviewResult) -> str:
    sessions = result.provenance["session_ids"]
    return review_marker(result.head, result.kind, "repost") + "\n" + (
        "Reposted from runner session" + ("s" if len(sessions) != 1 else "")
        + ": " + ", ".join(f"`{session}`" for session in sessions)
    )


def _recover_review(wt: Path, config: Config, pr: github.PR, result: ReviewResult,
                    publisher: str, token: str) -> tuple[bool, list[dict]]:
    with github.bounded_reads(time.monotonic() + 30, token=token, retry=True):
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


def _delta_patch(wt: Path, config: Config, since: str | None, head: str, number: int | None) -> bytes:
    if since is None:
        return b""
    if not _is_ancestor(wt, since, head):
        raise CliError(
            f"reviewed head {since[:12]} is not an ancestor of this branch. Find the original tip with `git reflog`. "
            "Back up your branch, reset to that tip and replay later fixes. To publish the restored head, "
            f"run `loopzero pr`, then `loopzero review` before merging {config.base_branch}, or open a new PR"
        )
    if not worktree.merges_since(wt, since):
        return worktree.diff_since(wt, since)
    patch = (eligibility.base_merge_material(config.repo, number, since, head, config.base_branch, clean=False)
             if number is not None else None)
    if patch is None:
        raise CliError(
            f"could not verify merge history: {since[:12]} must be on the first-parent history and "
            f"merged parents must come from {config.base_branch}. Save your work, then replay the fixes "
            f"on the reviewed head {since[:12]}, including resolution edits. Push with `loopzero pr` "
            "and run `loopzero review`, or open a new PR"
        )
    return patch


def _run_review(
    wt: Path, config: Config, head: str, kind: str, reviewed: str | None,
    model: str | None = None, effort: str | None = None, *, runtime_source: str = "",
    publication: tuple[str, int, str] | None = None,
) -> ReviewResult:
    lineage_base = _primary_base(wt, config)
    since = reviewed if kind == "delta" else lineage_base
    patch = (_delta_patch(wt, config, since, head, publication[1] if publication else None)
             if kind == "delta" else worktree.diff_since(wt, since))
    diff = runners.render_patch(patch)
    task = worktree.task_text(wt)
    authors = runners.author_families(wt, head, lineage_base)
    candidates = [family for family in config.reviewers if family not in authors]
    errors: dict[str, tuple[str, str]] = {}
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
                             "diff_sha256": hashlib.sha256(patch).hexdigest(),
                             "diff_bytes": len(patch)} if publication and family == "codex" else None),
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
            errors[family] = unusable[family], ""
            continue
        if family in excluded and not fallback:
            fallback = ("independent reviewers failed: " + "; ".join(reasons)
                        if candidates else "every configured family contributed (" + ", ".join(excluded) + ")")
            print(f"same-family fallback: {fallback}", file=sys.stderr)
        try:
            try:
                result = selected[family](diff=diff)
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
            errors[family] = detail, exc.tail if isinstance(exc, runners.RunnerMalformedOutput) else ""
            reasons.append(f"{family}: {fallback_reason}")
            if any(next_family in selected for next_family in attempts[index + 1:]):
                print(f"reviewer {family} unavailable: {reason}", file=sys.stderr)
    else:
        raise IndependentReviewerUnavailable(
            _independence_message(authors, excluded, [detail for detail, _ in errors.values()])
            + _save_review_failure_output(wt, head, kind, errors)
        )
    return dataclasses.replace(result, fallback=fallback)


def _save_review_failure_output(wt: Path, head: str, kind: str, errors: dict[str, tuple[str, str]]) -> str:
    if not (fragments := {family: tail for family, (_, tail) in errors.items() if tail}):
        return ""
    path = wt / ".loopzero" / f"reviewer-output-{head[:12]}-{kind}.json"
    try:
        worktree.write_local_state(wt, path.name, json.dumps(fragments, indent=2) + "\n", private=True)
    except (OSError, worktree.WorktreeError) as exc:
        return f"\nCould not save reviewer output: {_one_line(exc)}"
    return f"\nReviewer output: {path.relative_to(wt)}"


def _review_failure(family: str, exc: Exception) -> tuple[str, str, str]:
    lines = str(exc).splitlines()
    detail = "\n".join(lines[:1 if isinstance(exc, runners.RunnerMalformedOutput) else 3])
    if isinstance(exc, runners.RunnerAuthFailed):
        login = "claude auth login" if family == "claude" else "codex login"
        return lines[0], detail + f"\nFix: run `{login}`.", _FALLBACK_REASONS[0]
    if isinstance(exc, runners.RunnerUsageLimit):
        fix = "\nFix: wait for the limit to reset or switch this reviewer's account."
        return lines[0], detail + fix, _FALLBACK_REASONS[1]
    if isinstance(exc, runners.RunnerMissing):
        package = "@anthropic-ai/claude-code" if family == "claude" else "@openai/codex"
        return lines[0], detail + f"\nFix: install it with `npm install -g {package}`.", _FALLBACK_REASONS[2]
    return lines[0], detail, _FALLBACK_REASONS[3]


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
    data = dataclasses.asdict(result) | ({"publication": publication} if publication is not None else {})
    worktree.write_local_state(wt, path.name, json.dumps(data, indent=2) + "\n", private=True)


def _load_review(
    wt: Path, head: str, kind: str, *, publication: dict | None = None,
) -> tuple[ReviewResult, object]:
    path = _review_file(wt, head, kind)
    try:
        data = json.loads(worktree.read_local_state(wt, path.name))
        if not isinstance(data, dict):
            raise ValueError("review receipt must be an object")
        if data.get("head") != head:
            raise CliError(f"{path} holds a review of {str(data.get('head'))[:12]}, not HEAD {head[:12]}")
        saved_publication = data.pop("publication", None)
        if data.get("kind") != kind or (publication is not None and saved_publication != publication):
            raise ValueError("saved review kind or publication mismatch")
        findings = tuple(Finding(**f) for f in data["findings"])
        # Ordinary receipts written before pair retirement still carry these defaults.
        for field, default in (("members", []), ("reconciliation", ""),
                               ("pair_authority", None), ("pair_prompt_sha256", None)):
            if data.pop(field, default) != default:
                raise ValueError("retired paired review")
        result = ReviewResult(**{**data, "findings": findings})
    except FileNotFoundError:
        raise CliError(f"no saved {kind} review for {head[:12]} at {path}; run without --repost") from None
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise CliError(f"{path} is not runner-produced: invalid saved review") from exc
    if not runners.validate_runner_result(result):
        raise CliError(f"{path} is not runner-produced: provenance/envelope mismatch")
    return result, saved_publication


def _pr_readiness(
    wt: Path, config: Config, branch: str, *, allow_merged: bool = False, workflow_wait: bool = False
) -> tuple[github.PR, github.Readiness | None]:
    """PR plus readiness; readiness is None only for an already merged PR (allow_merged)."""
    head = worktree.head(wt)
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
        name = worktree.branch(wt)
        worktree.cleanup(wt, pr.head_sha)
    except (worktree.WorktreeError, OSError) as exc:
        retry = CLEANUP_RETRY if wt.exists() else ""
        if not retry and name is not None:
            retry = f"; worktree removed; delete local branch {name!r} if it still points at the merged head"
        print(f"warning: merged but worktree cleanup failed: {exc}{retry}", file=sys.stderr)


_SIZE_BUDGET = "scripts/size_budget.sh"


def _prequeue_size_budget(
    wt: Path, config: Config, pr: github.PR, readiness: github.Readiness, *, project: bool = True,
) -> None:
    if not readiness.ready or config.merge_strategy != "mergify" or _SIZE_BUDGET not in config.checks:
        return
    origin = _git(wt, "remote", "get-url", "origin")
    with tempfile.TemporaryDirectory(prefix="loopzero-size-") as directory:
        root = Path(directory)
        env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
               "PYTHON": sys.executable, "PYTHONSAFEPATH": "1"}
        git = partial(_git, root, extra_env=env)
        git("clone", "--shared", "--no-checkout", "--template=", str(wt), ".")
        # Fetch with the user's credential configuration for private origins.
        fetched = _proc.run(
            ["git", "fetch", "--no-tags", "--no-recurse-submodules", "--",
             origin, f"refs/heads/{config.base_branch}"],
            cwd=root, env_allowlist=worktree.GIT_ENV, timeout=GIT_TIMEOUT,
        )
        if fetched.exit_code != 0:
            raise CliError(f"could not fetch {config.base_branch} from origin for the size budget check")
        base = git("rev-parse", "FETCH_HEAD^{commit}")
        script = git("show", f"{base}:{_SIZE_BUDGET}")

        def evaluate(target: str) -> _proc.Completed:
            tree = git("merge-tree", "--write-tree", target, pr.head_sha)
            # A candidate symlink must not read files outside the temporary checkout.
            git("-c", "core.symlinks=false", "read-tree", "--reset", "-u", tree)
            return _proc.run(["bash", "-s", "--", base], input=script + "\n", cwd=root,
                             env_allowlist=("PATH",), extra_env=env, timeout=GIT_TIMEOUT,
                             merge_output=True)

        result = evaluate(base)
        if result.exit_code:
            raise CliError("current target size budget failed:\n" + result.stdout.rstrip())
        if project:
            _warn_queue_size(config, pr, root, origin, base, git, evaluate)


def _warn_queue_size(
    config: Config, pr: github.PR, root: Path, origin: str, base: str,
    git: Callable[..., str], evaluate: Callable[[str], _proc.Completed],
) -> None:
    assert config.mergify_queue is not None
    try:
        numbers = mergify.queue_predecessors(config.repo, config.base_branch, config.mergify_queue, pr.number)
        if not numbers:
            return
        refs = [f"refs/pull/{number}/head:refs/loopzero/{number}" for number in numbers]
        _git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--", origin, *refs)
        integrated = base
        for number in numbers:
            source = git("rev-parse", f"refs/loopzero/{number}^{{commit}}")
            if git("merge-base", integrated, source) == source:
                return  # A queued PR may already have landed or be represented by another head.
            tree = git("merge-tree", "--write-tree", integrated, source)
            integrated = git("-c", "user.name=loopzero", "-c", "user.email=loopzero@localhost",
                             "commit-tree", tree, "-p", integrated, "-p", source, "-m", "Queue size projection")
        result = evaluate(integrated)
    except (LoopZeroError, OSError):
        return
    if result.exit_code and "size budget: FAIL" in result.stdout:
        predecessors = ", ".join(f"#{number}" for number in numbers)
        print(f"warning: projected size budget after {predecessors}:\n{result.stdout.rstrip()}")


def cmd_ready(args: argparse.Namespace) -> int:
    wt, config = _context(args, pin_checks=True)
    read_started = time.monotonic()
    pr, readiness = _pr_readiness(wt, config, worktree.branch(wt), workflow_wait=args.wait is not None)
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
                                     allow_skipped=True, initial=None if kicked else readiness,
                                     initial_read_time=started - read_started)
        if waited is None:
            return 3
        pr, readiness, kicked = waited
    assert readiness is not None
    _prequeue_size_budget(wt, config, pr, readiness)
    if readiness.ready and pr.is_draft and not kicked:
        github.mark_ready(config.repo, pr.number)
        if args.wait is not None:
            waited = _wait_with_deadline(wt, config, pr, args.wait, kicked=True, started=started,
                                         args=args, allow_skipped=True,
                                         initial_read_time=started - read_started)
            if waited is None:
                return 3
            pr, readiness, kicked = waited
    assert readiness is not None
    for reason in _readiness_messages(pr, readiness):
        print(f"not ready: {reason}")
    if not readiness.ready:
        if args.wait is None and _pending_checks(config, pr, readiness):
            print(NEXT_WAIT)
        return 1
    print(f"ready: {pr.url}")
    return 0


def _required_check_reasons(names: tuple[str, ...], *states: str) -> dict[str, str]:
    return {f"required check '{name}' {state}": name for name in names for state in states}


def _draft_checks_waiting(
    config: Config, pr: github.PR, readiness: github.Readiness
) -> tuple[str, ...]:
    reasons = set(readiness.reasons) - {MERGEABILITY_PENDING}
    if not pr.is_draft or not reasons:
        return ()
    expected = _required_check_reasons(config.required_ci, f"missing on {pr.head_sha[:12]}", "is skipped")
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
    waitable = {*_required_check_reasons(config.required_ci, *states),
                *readiness.waitable_failures, MERGEABILITY_PENDING}
    reasons = readiness.reasons
    return reasons if reasons and all(r in waitable for r in reasons) else ()


def _readiness_messages(pr: github.PR, readiness: github.Readiness) -> Iterator[str]:
    for reason in readiness.reasons:
        check, separator, conclusion = reason.rpartition("' is ")
        if (separator and check.startswith("required check '") and conclusion not in {"pending", "skipped"}
                and reason not in readiness.waitable_failures):
            yield (f"{reason}. Inspect {pr.url}/checks. Fix reproducible failures before retrying. "
                   "Rerun the failed job only for a transient failure. Once green, run `loopzero ready --wait`.")
        else:
            yield reason


def _merge_check_hint(config: Config, pr: github.PR, readiness: github.Readiness) -> str:
    if _pending_checks(config, pr, readiness):
        return "; next: loopzero merge --wait"
    if _draft_checks_waiting(config, pr, readiness):
        return "; next: loopzero ready --wait"
    return ""


def _poll_until(
    started: float, timeout: float, *, bounded: bool = False,
    initial_read_time: float = 0.0, admitted: Callable[[], bool] | None = None,
) -> Iterator[None]:
    delay = WAIT_INTERVAL
    longest_read = initial_read_time
    while True:
        was_admitted = admitted is not None and admitted()
        read_started = time.monotonic()
        yield  # Observe immediately, including --wait=0.
        now = time.monotonic()
        longest_read = max(longest_read, now - read_started)
        remaining = timeout - (now - started)
        if remaining <= 0:
            return
        if admitted is not None and not was_admitted and admitted():
            delay = WAIT_INTERVAL
        pause = delay * random.uniform(0.9, 1.0)
        if bounded and remaining <= pause:
            # Poll late enough to see a settled check while reserving time to read it.
            baseline = min(5.0, max(0.5, remaining / 10))
            allowance = min(remaining / 2, max(baseline, 2 * longest_read))
            _sleep(remaining - allowance)
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
    initial_read_time: float = 0.0,
) -> tuple[github.PR, github.Readiness | None, bool] | None:
    started = time.monotonic() if started is None else started
    wait = partial(_wait_for_checks, wt, config, pr, timeout, kicked=kicked, started=started,
                   shown=shown, args=args, allow_merged=allow_merged,
                   allow_skipped=allow_skipped, initial=initial)
    if timeout == 0:
        return wait()
    deadline = started + timeout
    if time.monotonic() >= deadline:
        _print_check_wait_timeout(pr, args)
        return None
    reported_timeout = False
    try:
        with github.bounded_reads(deadline, token=None, retry=True):
            waited = wait(bounded=True, initial_read_time=initial_read_time)
            reported_timeout = waited is None
    except github.ReadDeadline:
        if not reported_timeout:
            _print_check_wait_timeout(pr, args)
        return None
    return waited


def _print_check_wait_timeout(pr: github.PR, args: argparse.Namespace | None) -> None:
    waiting_for = "GitHub to compute mergeability" if pr.mergeable == "UNKNOWN" else "required checks"
    command = ["loopzero", args.command if args else "ready", "--wait"]
    if args and args.config:
        command.insert(1, f"--config={args.config}")
    print(
        f"timed out waiting for {waiting_for} on {pr.head_sha[:12]}. "
        f"Run `{shlex.join(command)}` to resume on this head."
    )

def _verify_wait_snapshot(
    args: argparse.Namespace | None, wt: Path, config: Config, head: str, current_head: str,
    previous: Config | None = None,
) -> Config:
    if current_head != head:
        raise CliError(f"PR head changed while waiting: {head[:12]} to {current_head[:12]}")
    if args is None:
        return config
    latest = _load_config(args, wt, pin_checks=True, previous=previous or config)
    changed = "workflow settings"
    if latest == config:
        if (latest.trusted_base_revision == config.trusted_base_revision
                or not (config.check_runner or config.inputs)):
            return latest
        changed = "check base"
    if _current_check_report(wt, latest, head) is None:
        raise CliError(f"{changed} changed while waiting; run loopzero check and retry")
    raise CliError(f"{changed} changed while waiting; retry with the current settings")


def _print_wait_changes(shown: tuple[str, ...], pending: tuple[str, ...]) -> None:
    def by_check(reasons: tuple[str, ...]) -> dict[str, str]:
        return {reason.rpartition("' ")[0] or reason: reason for reason in reasons}
    previous, current = by_check(shown), by_check(pending)
    if changed := [reason for key, reason in current.items() if previous.get(key) != reason]:
        print("waiting: " + "; ".join(changed))
    if cleared := [key.removeprefix("required check '") for key in previous if key not in current]:
        print("cleared: " + "; ".join(cleared))


def _wait_for_checks(
    wt: Path, config: Config, pr: github.PR, timeout: float, *, kicked: bool,
    started: float | None = None, shown: tuple[str, ...] = (),
    args: argparse.Namespace | None = None, bounded: bool = False, allow_merged: bool = False,
    allow_skipped: bool = False, initial: github.Readiness | None = None,
    initial_read_time: float = 0.0,
) -> tuple[github.PR, github.Readiness | None, bool] | None:
    """Poll the same head until it settles, omitting readiness for an allowed merge."""
    grace_started = time.monotonic()
    started = grace_started if started is None else started
    current = pr
    stale_notice = False
    previous = config
    for _ in _poll_until(started, timeout, bounded=bounded, initial_read_time=initial_read_time):
        if initial is not None:
            readiness = initial
            initial = None
        else:
            current = github.pr_view(config.repo, pr.number)
            previous = _verify_wait_snapshot(args, wt, config, pr.head_sha, current.head_sha, previous)
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
        if pending or readiness.ready:
            _print_wait_changes(shown, pending)
        if not pending:
            return current, readiness, kicked
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
    _print_check_wait_timeout(current, args)
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
        labels = [label.encode() for label in state.unmet_conditions[:4]]
        shown = [label[:160].decode(errors="ignore") + ("…" if len(label) > 160 else "")
                 for label in labels]
        detail += ". Unmet candidate conditions: " + "; ".join(shown)
        if omitted := len(state.unmet_conditions) - len(shown):
            detail += f"; {omitted} more"
    return detail


MERGIFY_REQUEUE = (
    "To requeue, post `@mergifyio queue` as the whole PR comment "
    "(same login), then rerun `loopzero merge --wait`."
)
MERGIFY_DEQUEUE_HINT = (
    'Read Mergify\'s "Left the queue" comment on the PR for the reason. Address it before requeueing.'
)


def _mergify_departure_hint(report: mergify.Departure | None, base: str) -> str:
    if report is None:
        return f"{MERGIFY_DEQUEUE_HINT} {MERGIFY_REQUEUE}"
    detail = f"Mergify report: {report.url}."
    if report.attribution == "base_conflict":
        return (f"The PR conflicts with {base}. Run `git fetch origin {base}` and resolve the base "
                f"integration according to the PR's review state. Requeue only after resolving the "
                f"conflict. {detail} {MERGIFY_REQUEUE}")
    if report.checks:
        detail += " Failing checks: " + ", ".join(repr(check) for check in report.checks) + "."
    guidance = {
        "stacked": "Mergify tested a stack and could not attribute the failure. Inspect the candidate in the report or requeue after the earlier PRs clear.",
        "isolated": "Mergify tested this PR alone. Inspect or fix the failed check before requeueing unless it is a known flake.",
    }
    return f"{detail} {guidance.get(report.attribution, MERGIFY_DEQUEUE_HINT)} {MERGIFY_REQUEUE}"


def _mergify_departure(
    config: Config, pr: github.PR, queue: str, closed: list[github.GhError], confirmed: bool,
) -> str | None:
    report = mergify.departure_report(config.repo, pr.number, pr.head_sha, queue)
    if not confirmed and report is None:
        return None
    sha = _landed(config, pr, closed)
    if not sha and not closed:
        raise CliError(
            f"PR #{pr.number} left Mergify queue {queue} unmerged. {_mergify_departure_hint(report, pr.base_ref)}"
        )
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
    polls = _poll_until(started, timeout, admitted=lambda: confirmed)
    for _ in polls:
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
        elif state is None:
            if sha := _mergify_departure(config, pr, queue, closed, confirmed):
                return sha
    # A queued, open PR normally gets its next landing read on the next tick.
    if deferred_landing_read and (sha := _landed(config, pr, closed)):
        return sha
    if closed:
        raise closed[0]
    print(f"timed out waiting for Mergify to land PR #{pr.number}")
    print(_mergify_timeout_detail(config, pr, queue, state))
    return None


_FOLLOW_QUEUE = "next: loopzero merge --wait follows the queue, verifies and cleans up"


class _AdmissionDeadline(Exception):
    """The --wait budget expired before a new merge or queue request."""


def _merge_with_mergify(
    wt: Path, config: Config, pr: github.PR,
    readiness: github.Readiness, deadline: float | None, admit_by: float | None = None,
) -> str | None:
    """Request Mergify after readiness verifies the source gates and queue configuration."""
    if not readiness.ready:
        hint = _merge_check_hint(config, pr, readiness)
        raise CliError("not ready to request Mergify: " + "; ".join(_readiness_messages(pr, readiness)) + hint)
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
    if not confirmed and confirmed_marker:
        closed: list[github.GhError] = []
        if sha := _mergify_departure(config, pr, queue, closed, confirmed=True):
            return sha
        raise closed[0]
    if not confirmed and not requested:
        _prequeue_size_budget(wt, config, pr, readiness, project=admit_by is None)
        if admit_by is not None and time.monotonic() > admit_by:
            raise _AdmissionDeadline
        mergify.request(config.repo, pr.number, pr.head_sha, queue)
    if deadline is not None:
        return _wait_for_mergify(config, pr, max(0.0, deadline - time.monotonic()), confirmed)
    state_name = "confirmed in" if confirmed else "requested from"
    print(f"PR #{pr.number} {state_name} Mergify queue {queue}; {_FOLLOW_QUEUE}")
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
    read_started = time.monotonic()
    pr, readiness = _pr_readiness(
        wt, config, branch, allow_merged=True, workflow_wait=args.wait is not None
    )
    started = time.monotonic()
    deadline = None if args.wait is None else started + args.wait
    # --wait=0 observes once; a positive budget also bounds merge or queue admission.
    admit_by = deadline if args.wait else None
    if readiness is not None and args.wait is not None and _pending_checks(config, pr, readiness):
        # One waiter and one deadline span source checks and landing: a check created
        # by the ready transition must not end the turn between `ready` and the queue.
        waited = _wait_with_deadline(wt, config, pr, args.wait, kicked=False, started=started,
                                     args=args, allow_merged=True, initial=readiness,
                                     initial_read_time=started - read_started)
        if waited is None:
            return 3
        pr, readiness, _ = waited
    if readiness is None:
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha)
        if sha is None:
            raise CliError(f"PR #{pr.number} reports MERGED but has no merge commit yet; rerun")
        print(f"PR #{pr.number} was already merged as {sha[:12]}; cleaning up")
    elif config.merge_strategy == "mergify":
        try:
            sha = _merge_with_mergify(wt, config, pr, readiness, deadline, admit_by)
        except _AdmissionDeadline:
            print(f"timed out before requesting Mergify for PR #{pr.number}")
            return 3
        if sha is None:
            return 3 if args.wait is not None else 0
    else:
        if not readiness.ready:
            hint = _merge_check_hint(config, pr, readiness)
            raise CliError("not ready to merge: " + "; ".join(_readiness_messages(pr, readiness)) + hint)
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
            print(f"PR #{pr.number} is queued for merge into {config.base_branch}; {_FOLLOW_QUEUE}")
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
    wt, config = _context(args, pin_checks=True)  # publishers come from the base, not the PR
    pr = _require_pr(config, worktree.branch(wt))
    if pr.base_ref != config.base_branch:  # trusted publishers belong to the configured base
        raise CliError(f"PR #{pr.number} targets {pr.base_ref}, configured base is {config.base_branch}")
    publishers = config.review_publishers or (github.login(),)
    if not args.finding:
        for marker_id, _thread, f in github.open_findings(config.repo, pr.number, publishers):
            print(f"{marker_id}  {f.severity}  {f.path or '-'}:{f.line or '-'}  {f.title}")
        return 0
    finding = github.resolve_finding(config.repo, pr.number, args.finding, args.reply, publishers)
    print(f"resolved {args.finding}: {finding.title}")
    try:
        with github.api_error_diagnostics():
            remaining = github.open_blocking_findings(config.repo, pr.number, None, publishers)
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


def main(argv: list[str] | None = None) -> int:
    return _run(parse_args(build_parser(), argv))


def _run(args: argparse.Namespace) -> int:
    try:
        with _proc.unwind_on_sigterm():
            return globals()[f"cmd_{args.command}"](args)
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
    sys.exit(_run(_module_args))
