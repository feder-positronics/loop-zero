"""Command line: composes the modules into start, check, pr, review, ready, merge, resolve, status."""

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
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import ExitStack, nullcontext, suppress
from functools import partial
from pathlib import Path
from urllib.parse import quote

from loopzero import _proc, eligibility, github, mergify, runners, sandbox, worktree
from loopzero import config as config_mod
from loopzero.types import CheckReport, CheckResult, Config, Finding, LoopZeroError, ReviewConfig, ReviewResult, is_check_exit_code

CHECKS_FILE = Path(".loopzero") / "checks.json"
GIT_TIMEOUT = 60.0
RUNNER_FAILURES = (runners.RunnerMissing, runners.RunnerAuthFailed, runners.RunnerBadOutput)
HANDLED = (LoopZeroError, OSError, ValueError)
# Each readiness poll spends shared GitHub API quota.
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


class IndependentReviewerUnavailable(CliError):
    """No independent configured model family can provide the review."""


def review_marker(head: str, kind: str, source: str = "model") -> str:
    return f"<!-- loopzero:review v=1 head={head} kind={kind} source={source} -->"


_git = partial(worktree.git, timeout=GIT_TIMEOUT)


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
    done = _proc.run(["git", *args], cwd=wt, env_allowlist=worktree.GIT_ENV, extra_env={"LC_ALL": "C"}, timeout=PUSH_TIMEOUT)
    if done.exit_code != 0:
        detail = _proc.tail(done.stderr)[-2000:]
        if force_lease and any(line.startswith("!\t") and line.endswith("\t[rejected] (stale info)")
                               for line in done.stdout.splitlines()):
            detail += "\nremote moved since PR lookup; fetch and retry"
        raise CliError(f"git {' '.join(args)} failed: {detail.strip()}")


def _toplevel() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel", paths=True))


class PrimaryCheckout(CliError):
    """The command runs in the primary checkout, which has no task worktree to remove."""


def _repo_root(wt: Path, *, linked_only: bool = True) -> Path:
    first, _, fields = _git(wt, "worktree", "list", "--porcelain", "-z", paths=True).partition("\0")
    if not first.startswith("worktree "):
        raise CliError("Git did not identify the primary worktree; refusing cleanup")
    root = Path(first.removeprefix("worktree ")).resolve()
    common = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", paths=True)).resolve()
    if root == common and not fields.startswith("bare\0"):
        candidates = (wt, wt.parent.parent) if wt.parent.name == ".worktrees" else (wt,)
        for candidate in candidates:
            if (candidate / ".git").is_file() and Path(_git(candidate, "rev-parse", "--absolute-git-dir", paths=True)).resolve() == common:
                root = candidate
                break
        else:
            raise CliError("Git reports only the primary metadata directory. Rerun from the primary checkout.")
    elif common not in (root, (root / ".git").resolve()):
        raise CliError("primary repository does not own this linked worktree; refusing cleanup")
    if linked_only and root == wt.resolve():
        raise PrimaryCheckout("this is the primary checkout, nothing to clean up")
    return root


def _publisher_problem(args: argparse.Namespace, config: Config) -> str | None:
    if config.review_publishers:
        return None
    return (f'{args.config or f"origin/{config.base_branch}:workflow.toml"}: delivery.review_publishers must list the '
            'intended publisher logins; add `review_publishers = ["<login>"]` under `[delivery]`')


def _load_config(
    args: argparse.Namespace, root: Path, *, previous: Config | None = None,
    refresh: bool = False,
) -> Config:
    require_ci = args.command in {"ready", "merge"} and not getattr(args, "cancel", False)
    if args.config:
        print("checks are unpinned because --config was provided")
        config = config_mod.load(args.config, require_ci=require_ci)
    else:
        config = config_mod.load_trusted(root, previous=previous, refresh=refresh,
                                         require_ci=require_ci, read_timeout=github.read_timeout)
        if args.command == "check":
            print(f"checks pinned to origin/{config.base_branch}@{config.trusted_base_revision}")
    if require_ci and (problem := _publisher_problem(args, config)):
        raise config_mod.ConfigError(problem)
    return config


def _context(args: argparse.Namespace) -> tuple[Path, Config]:
    wt = _toplevel()
    return wt, _load_config(args, wt, refresh=args.command in {"ready", "merge"})


def _is_ancestor(wt: Path, sha: str, head: str) -> bool:
    done = config_mod.run_git(wt, "merge-base", "--is-ancestor", sha, head)
    if done.exit_code not in (0, 1):
        detail = _proc.tail(done.stderr or done.stdout, 1)
        raise CliError(f"git merge-base --is-ancestor failed (exit {done.exit_code}): "
                       f"{detail or 'retry the ancestry check'}. If the commit is missing, run `git fetch origin {sha}` and retry.")
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


def _require_pr(config: Config, branch: str, *, allow_finished: bool = False, head_sha: str | None = None) -> github.PR:
    pr = github.pr_for_branch(config.repo, branch, head_sha=head_sha)
    if pr is None:
        raise CliError(f"no pull request for {branch}; run `loopzero pr` first")
    if pr.state != "OPEN" and not (allow_finished and pr.state in {"MERGED", "CLOSED"}):
        raise CliError(f"PR #{pr.number} is {pr.state}: {pr.url}. Reopen a closed unmerged PR or run `loopzero start` for a new task.")
    return pr


def _require_pushed(pr: github.PR, head: str) -> None:
    if pr.head_sha != head:
        raise CliError(f"PR head {pr.head_sha[:12]} differs from local {head[:12]}; push or pull first")


def _pr_reviews(repo: str, number: int, **kwargs) -> list[dict]:
    return github.paged(f"repos/{repo}/pulls/{number}/reviews", **kwargs)


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
                runners.codex_fatal_startup(item) for item in eligibility.review_envelopes(body)):
            continue
        match = eligibility.leading_marker(body)
        if not match or review.get("commit_id") != match[1] or (
            review.get("state") == "COMMENTED" and not eligibility.published_verdict(body, match[1], match[2])
        ):
            continue
        if not trusted:
            trusted = {github.login().casefold()}
        if eligibility.review_login(review) not in trusted:
            continue
        found.append(Marker(match.group(1), match.group(2), review.get("state") or ""))
    return found


def _review_cap_spent(markers: list[Marker]) -> bool:
    return len({m.head for m in markers}) >= 2 or {m.kind for m in markers} >= {"primary", "delta"}


def _decide_kind(markers: list[Marker], head: str) -> tuple[str, str | None]:
    """Return (kind, reviewed_head) or raise when the PR's review budget is spent."""
    current = next((m for m in reversed(markers) if m.head == head), None)
    if current and current.state == "DISMISSED":
        raise CliError("review for the current head was dismissed. Push fixes for a remaining delta "
                       "or open a replacement PR if the review budget is spent.")
    if _review_cap_spent(markers):
        raise CliError("review cap reached: fix or reject the findings (resolve-findings) and run `loopzero ready`, or split the PR")
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
    if (not eligibility.approved_delta(reviews, reviewed, publishers, repo=config.repo, number=pr.number)
            or eligibility.base_merge_material(
                config.repo, pr.number, reviewed, head, config.base_branch, clean=False, conflict_only=True,
            ) is None or github.open_blocking_findings(config.repo, pr.number, None, publishers)):
        return None
    return reviewed


def _select_review_round(config: Config, pr: github.PR, head: str, reviews: list[dict]) -> tuple[str, str | None]:
    publishers = config.review_publishers
    markers = _pr_markers(reviews, head, publishers, repo=config.repo, number=pr.number)
    if reviewed := _conflict_delta_base(config, pr, head, reviews, markers, publishers):
        return "delta", reviewed
    return _decide_kind(markers, head)


def _readiness(
    wt: Path, config: Config, pr: github.PR, head: str, reviews: list[dict] | None = None,
    *, workflow_wait: bool = False, announce_receipt: bool = False, diagnostics: bool = False,
    mergify_deadline: float | None = None, dates: tuple[str, ...] = (),
) -> github.Readiness:
    if problem := github.merge_hold(pr.number, pr.labels) or (
        None if diagnostics else _check_report_problem(wt, _load_report(wt), config, head)
    ):
        return github.Readiness(False, (problem,))
    reviews = _pr_reviews(config.repo, pr.number, dates=(dates := [])) if reviews is None else reviews
    publishers = config.review_publishers
    review = eligibility.readiness_review(
        reviews, head, publishers, repo=config.repo, number=pr.number, base_branch=config.base_branch,
    )
    review_at = github.readiness_timestamp(review.get("submitted_at")) if review else None
    server = github.response_date(dates[-1]) if dates else None
    now = server.timestamp() if server else time.time()
    review_age = now - review_at[0].timestamp() if review_at else None
    if server and review_age is not None and -1 < review_age < 0:
        review_age = 0  # HTTP Date rounds down to the second.
    review_blocker = eligibility.body_blocker(review)
    result = github.readiness(
        config.repo, pr, config.base_branch, config.required_ci,
        head if review else None, review_at=review_at, review_blocker=review_blocker,
        ancestor_review=review is not None and review["commit_id"] != head,
        review_grace=review_age is not None and 0 <= review_age < MISSING_RUN_GRACE,
        workflow_wait=workflow_wait,
        stale_verdict_grace=(
            workflow_wait and review_age is not None
            and (0 if server else -MAX_REVIEW_CLOCK_SKEW) <= review_age < STALE_VERDICT_GRACE
        ),
        publishers=publishers,
    )
    if config.merge_strategy == "mergify" and pr.state == "OPEN":
        behind = github.behind_reason(pr)
        reasons = result.reasons
        if mergify.configured(config.repo, config.base_branch, config.mergify_queue or "", mergify_deadline):
            reasons = tuple(reason for reason in reasons if reason != behind)
        else:
            reasons = (*reasons, "Mergify queue configuration is unverified or permits in-place head updates. Verify the queue in Mergify or ask its maintainer to disable source branch updates.")
        result = dataclasses.replace(result, ready=not reasons, reasons=reasons)
    review_reason = review_blocker or github.MISSING_REVIEW_REASON
    if review_reason in result.reasons:
        markers = _pr_markers(reviews, head, publishers, repo=config.repo, number=pr.number)
        if _review_cap_spent(markers) and (review_blocker or all(marker.head != head for marker in markers)):
            if _conflict_delta_base(config, pr, head, reviews, markers, publishers):
                exhausted = f"Run `loopzero review` for the available conflict-resolution delta on {head[:12]}."
            else:
                context = (f"{review_blocker.capitalize()}. The review budget is exhausted." if review_blocker
                           else f"The review budget is exhausted and {head[:12]} has no eligible review.")
                exhausted = (
                    f"{context} "
                    "Open a replacement PR for changes that need review. Another `loopzero review` on this PR will be refused."
                )
            result = dataclasses.replace(result, reasons=tuple(
                exhausted if reason == review_reason else reason for reason in result.reasons
            ))
    report = _load_report(wt)  # deliberate re-read: a check can land during the GitHub reads
    if receipt_problem := _check_report_problem(wt, report, config, head):
        return github.Readiness(False, (*result.reasons, receipt_problem), checks=result.checks)
    if announce_receipt:
        _print_receipt(wt, report, "accepted")
    return _waitable_eligibility_refresh(config, pr, result, workflow_wait=workflow_wait)


def _waitable_eligibility_refresh(
    config: Config, pr: github.PR, readiness: github.Readiness, *, workflow_wait: bool,
) -> github.Readiness:
    if not workflow_wait:
        return readiness
    reason = next((r for r, check in readiness.checks.items()
                   if check in {(eligibility.CONTEXT, "failure"), (eligibility.CONTEXT, "error")}), None)
    if reason is None or reason in readiness.waitable_failures:
        return readiness
    waiting = dataclasses.replace(readiness, waitable_failures=(*readiness.waitable_failures, reason))
    if not _pending_checks(waiting):
        return readiness
    branch = quote(config.base_branch, safe="")
    endpoint = f"repos/{config.repo}/actions/workflows/eligibility.yml/runs"
    try:
        runs = github.api_get(f"{endpoint}?event=workflow_dispatch&branch={branch}&per_page=100")
    except github.ReadDeadline:
        raise
    except github.GhError:
        return readiness  # Unavailable workflow metadata cannot soften a failed verdict.
    rows = runs.get("workflow_runs") if isinstance(runs, dict) else None
    if isinstance(rows, list) and any(
        isinstance(run, dict) and run.get("display_title") == f"Refresh eligibility for PR #{pr.number}"
        and run.get("status") in ("requested", "queued", "waiting", "pending", "in_progress") for run in rows
    ):
        return waiting  # The next poll still reads required checks on the pinned PR head.
    return readiness


def _check_report_problem(
    wt: Path, report: CheckReport | None, config: Config, head: str, *, require_pass: bool = True,
) -> str | None:
    if report is None:
        return "run loopzero check at this head"
    if report.head != head:
        return f"check receipt is for {report.head[:12]}. Run `loopzero check` at {head[:12]}."
    if report.dirty:
        return "check receipt was recorded in a dirty worktree. Run `git status --short`, settle the changes, then run `loopzero check`."
    commands = tuple(result.command for result in report.results)
    if not commands or commands[-1] == sandbox.INCOMPLETE_CHECK_COMMAND:
        return "check receipt is incomplete. Run `loopzero check` to resume with the current workflow."
    expected = (config.checks,)
    if not report.ok:
        if require_pass:
            return ("check receipt records a failed stage. Fix the failure, then run `loopzero check`. "
                    "Retry unchanged only if the failure was transient.")
        missing, preparations = sandbox.check_preflight(config, wt)
        expected = ((missing.command,),) if missing else (config.checks[:len(commands)], preparations)
    if commands not in expected:
        return "check receipt's plan differs from the trusted workflow. Run `loopzero check` with the current workflow."
    if report.settings_sha256 is None or all(
        report.settings_sha256 != sandbox.settings_fingerprint(config, legacy_allowlist=names)
        for names in (None, config.env_allowlist, tuple(n for n in config.env_allowlist if n != sandbox.CPU_GRANT_ENV))
    ):
        problem = "is missing its" if report.settings_sha256 is None else "has an incompatible"
        return (f"check receipt {problem} settings fingerprint. Rerun `loopzero check` in this worktree at this head "
                "with the same CLI version, environment and `--config` (if supplied) as the blocked command, then retry it.")
    if report.ok and not sandbox.check_inputs_match(config, wt, report, bytes_only=True):
        return "check receipt input bytes changed or could not be verified. Rerun `loopzero check` with `checks.inputs` that match readable, regular worktree files."
    return None


def _current_check_report(wt: Path, config: Config, head: str) -> CheckReport | None:
    report = _load_report(wt)
    return None if _check_report_problem(wt, report, config, head) else report


def _require_receipt(wt: Path, config: Config, head: str, *, suffix: str = "") -> None:
    if problem := _check_report_problem(wt, _load_report(wt), config, head):
        raise CliError(problem + suffix)


def _print_receipt(wt: Path, report: CheckReport, action: str) -> None:
    """Best-effort transcript identity; never authority or environment attestation."""
    with suppress(LoopZeroError, OSError):
        content = _git(wt, "rev-parse", f"{report.head}^{{tree}}")
        commands = json.dumps([r.command for r in report.results], separators=(",", ":"))
        digest = hashlib.sha256(commands.encode()).hexdigest()
        print(f"receipt {action}: head={report.head} content={content} "
              f"config=commands-sha256:{digest} environment=unknown")


def _save_report(
    wt: Path, report: CheckReport, *, checkpoint_checks: tuple[str, ...] | None = None, locked_fd: int | None = None,
) -> str | None:
    """Publish the receipt or return the current HEAD if the result is stale."""
    results = [
        {k: v for k, v in dataclasses.asdict(result).items()
         if k not in {"input_key", "reused_from"} or v is not None}
        for result in report.results
    ]
    payload = {"head": report.head, "dirty": report.dirty, "results": results}
    if report.settings_sha256 is not None:
        payload["settings_sha256"] = report.settings_sha256
    def preserve_checkpoint() -> bool:
        if report.results[-1] is not sandbox.INCOMPLETE_CHECK:
            return not worktree.local_state_files(wt, CHECKS_FILE.name)[0]
        saved = _load_report(wt)
        return (saved is not None and saved.ok and not saved.dirty and saved.head == report.head
                and saved.settings_sha256 == report.settings_sha256
                and tuple(result.command for result in saved.results) == checkpoint_checks)

    return worktree.write_local_state(
        wt, CHECKS_FILE.name, json.dumps(payload, indent=2) + "\n", expected_head=report.head, locked_fd=locked_fd,
        preserve=preserve_checkpoint if checkpoint_checks is not None else None,
    )


def _load_report(wt: Path) -> CheckReport | None:
    try:
        data = json.loads(worktree.read_local_state(wt, CHECKS_FILE.name))
        results = tuple(CheckResult(**item) for item in data["results"])
        if any(not is_check_exit_code(result.exit_code) for result in results):
            raise TypeError("exit_code must be an integer")
        if not isinstance(dirty := data["dirty"], bool):
            raise TypeError("dirty must be a bool")
        return CheckReport(head=str(data["head"]), dirty=dirty, results=results,
                           settings_sha256=data.get("settings_sha256"))
    except FileNotFoundError:
        return None
    except (TypeError, KeyError, ValueError) as exc:
        print(f"warning: ignoring unreadable {wt / CHECKS_FILE}: {exc}", file=sys.stderr)
        return None


def _validation_section(wt: Path, head: str, config: Config) -> str:
    loaded = _load_report(wt)
    if loaded is None:
        return "(no `loopzero check` run recorded)"
    heading = f"Recorded for `{loaded.head[:12]}`" + ("" if loaded.head == head else " (not current head)")
    if _check_report_problem(wt, loaded, config, head, require_pass=False):
        heading += " (unverified, run `loopzero check`)"
    results = []
    for result in loaded.results:
        command = github.redact_credentials(result.command)
        if any(token in command for token in ("\n", "\r", "`", "<!--", "~~~")):
            command = command.replace("\r\n", "\n").replace("\r", "\n")
            fence = "~" * max(3, max(map(len, re.findall(r"~+", command)), default=0) + 1)
            results.append(f"- exit {result.exit_code}\n\n{fence}sh\n{command}\n{fence}\n")
        else:
            results.append(f"- `{command}`: exit {result.exit_code}")
    return "\n".join([heading, *(results or ["- (no check commands configured)"])])


_CHECKS_START = "<!-- loopzero:checks:start -->"
_CHECKS_END = "<!-- loopzero:checks:end -->"
_SECTION_START = r"^ {0,3}## "
_SECTION_END = rf"(?={_SECTION_START}|\Z)"
_TASK_EVIDENCE = re.compile(rf"{_SECTION_START}(Validation|Review)[ \t]*(?:\n|\Z).*?{_SECTION_END}", re.MULTILINE | re.DOTALL)


def _outside_fences(body: str) -> str:
    # Keep character offsets while hiding Markdown fences and their contents.
    return re.sub(r"^ {0,3}((`|~)\2{2,}+)[^\n]*\n.*?(?:^ {0,3}\1\2*[ \t]*$|\Z)",
                  lambda match: " " * len(match[0]), body, flags=re.MULTILINE | re.DOTALL)


def _pr_body(wt: Path, head: str, config: Config, existing: str) -> str:
    """Refresh only generated checks; the live body owns existing PR prose."""
    body = existing.replace("\r\n", "\n")
    block = f"{_CHECKS_START}\n{_validation_section(wt, head, config)}\n{_CHECKS_END}"
    match = re.search(rf"{_SECTION_START}Validation[ \t]*\n(.*?){_SECTION_END}", _outside_fences(body), re.DOTALL | re.MULTILINE)
    if match is None:
        return _validate_pr_body_size(f"{body.rstrip()}\n\n## Validation\n{block}\n")
    content = body[match.start(1):match.end(1)]
    before, start, after = match[1].partition(_CHECKS_START)
    if start or _CHECKS_END in match[1]:
        middle, end, suffix = after.partition(_CHECKS_END)
        if not end or _CHECKS_END in before or _CHECKS_END in suffix or _CHECKS_START in after:
            raise CliError("malformed loopzero checks block: fix the marker pair in Validation in the PR description on GitHub")
        content = content[:len(before)] + block + content[len(before + start + middle + end):]
    else:
        # Replace only our placeholder, retaining behavioral evidence.
        placeholders = re.finditer(
            r"^\((?:filled by `loopzero check`|no `loopzero check` run recorded)\)\n?",
            match[1], flags=re.MULTILINE,
        )
        for placeholder in reversed(list(placeholders)):
            content = content[:placeholder.start()] + content[placeholder.end():]
        content = block + "\n" + ("\n" + content.lstrip("\n") if content.strip() else "\n")
    return _validate_pr_body_size(body[:match.start(1)] + content + body[match.end():])


def _validate_pr_body_size(body: str) -> str:
    limit = 65_536
    if len(body) > limit:
        raise CliError(f"generated PR body is {len(body)} characters; GitHub allows {limit}. "
                       "Shorten the PR description or validation text and rerun the command.")
    return body


def _seed_task_evidence(task: str, body: str) -> str:
    present = {section[1] for section in _TASK_EVIDENCE.finditer(_outside_fences(body))}
    missing = [task[section.start():section.end()].rstrip()
               for section in _TASK_EVIDENCE.finditer(_outside_fences(task)) if section[1] not in present]
    return "\n\n".join([body.rstrip(), *missing]) + "\n" if missing else body


def _sync_task_narrative(task: str, existing: str) -> str:
    """Merge each narrative part against its last shared text."""
    pattern = re.compile(rf"{_SECTION_START}([^\n]*)(?:\n|\Z).*?{_SECTION_END}", re.MULTILINE | re.DOTALL)

    def parts(body: str) -> dict[tuple[str | None, int], str]:
        prose = _outside_fences(body)
        prefix = body[:first.start()] if (first := pattern.search(prose)) else body
        result: dict[tuple[str | None, int], str] = {(None, 0): prefix.rstrip()}
        counts: dict[str, int] = {}
        for match in pattern.finditer(prose):
            name = match[1].rstrip(" \t")
            index = counts.get(name, 0)
            result[name, index] = body[match.start():match.end()].rstrip()
            counts[name] = index + 1
        return result

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    local = parts(task)
    live = parts(existing.replace("\r\n", "\n"))
    prefix = live[None, 0]
    marker = r"^<!-- loopzero:task-(?:sections [0-9a-f ]+|sync ((?:[0-9a-f]{16}:[0-9a-f]{16} )*))-->$"
    prior = re.search(marker, _outside_fences(prefix), re.MULTILINE)
    baseline = {}
    has_baseline = bool(prior and prior[1] is not None)
    if prior:
        live[None, 0] = (prefix[:prior.start()] + prefix[prior.end():]).rstrip()
        baseline = dict(pair.split(":") for pair in (prior[1] or "").split())

    content = []
    fingerprints = {}
    empty = digest("")
    for part in dict.fromkeys([*live, *local]):
        name, _ = part
        current, proposed = live.get(part, ""), local.get(part, "")
        if name in {"Validation", "Review"}:
            content.append(current)
            continue
        key = digest(repr(part))
        current_hash, proposed_hash = digest(current), digest(proposed)
        # Older bodies lack content fingerprints. Preserve overlapping live text on adoption.
        default = proposed_hash if not has_baseline and current else empty
        previous = baseline.get(key, default)
        if proposed_hash != previous and current_hash not in {previous, proposed_hash}:
            raise CliError(f"PR description conflict in {name or 'the preamble'}: "
                           "reconcile .loopzero/task.md with the GitHub PR body and rerun loopzero pr")
        content.append(current if proposed_hash == previous else proposed)
        if proposed:
            fingerprints[key] = proposed_hash

    metadata = "".join(f"{key}:{fingerprints[key]} " for key in sorted(fingerprints))
    content.insert(1, f"<!-- loopzero:task-sync {metadata}-->")
    return "\n\n".join(filter(None, content)) + "\n"


def _require_pr_details(wt: Path, task: str) -> None:
    """Report every unfilled PR description field before push."""
    unfilled = []
    for name in ("Context", "Problem", "Goal"):
        match = re.search(rf"^- \*\*{name}:\*\*[ \t]*(.*(?:\n[ \t]+\S.*)*)", task, re.MULTILINE)
        value = match.group(1).strip() if match else ""
        if not value or re.fullmatch(r"<[^<>]*>", value):
            unfilled.append(f"'- **{name}:**'")
    if unfilled:
        raise CliError(f"fill {', '.join(unfilled)} in {worktree.task_file(wt)}"
                       " (Context, Problem and Goal become the PR description)")


def _append_review(body: str, result: ReviewResult) -> str:
    """Append one compact review record under Review, replacing its placeholder."""
    line = f"- {result.kind}, {result.family}, {result.head[:12]}, {result.verdict}"
    match = re.search(rf"{_SECTION_START}Review[ \t]*\n(.*?){_SECTION_END}", _outside_fences(body), re.DOTALL | re.MULTILINE)
    if not match:
        return f"{body.rstrip()}\n\n## Review\n{line}\n"
    existing = re.sub(r"^\(filled by `loopzero review`\)[ \t]*(?:\n+|$)", "", body[match.start(1):match.end(1)]).rstrip()
    if existing:
        line = f"{existing}\n{line}"
    return (body[:match.start(1)] + line + "\n\n" + body[match.end():]).rstrip() + "\n"


def _refresh_pr_checks(wt: Path, config: Config) -> None:
    """Best-effort refresh only when local HEAD is the existing PR head."""
    try:
        upstream = config_mod.run_git(wt, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if upstream.exit_code != 0:
            return
        branch, head = worktree.branch(wt), worktree.head(wt)
        pr = github.pr_for_branch(config.repo, branch, open_only=True, head_sha=head)
        if pr is None or pr.head_sha != head:
            return
        with github.pr_lock(config.repo, pr.number):
            live = github.pr_view(config.repo, pr.number)
            if (live.state, live.head_sha, live.base_ref) != ("OPEN", head, config.base_branch):
                return
            body = _pr_body(wt, head, config, live.body)
            if body != live.body.replace("\r\n", "\n"):
                github.update_body(config.repo, pr.number, body)
    except (LoopZeroError, OSError, ValueError) as exc:
        print(f"warning: could not refresh PR checks: {_one_line(exc)}", file=sys.stderr)


def _pr_title(wt: Path, branch: str) -> str:
    headings = (line[2:].strip() for line in worktree.task_text(wt).splitlines() if line.startswith("# "))
    return next(filter(None, headings), branch)


def cmd_start(args: argparse.Namespace) -> int:
    root = _toplevel()
    current = config_mod.run_git(root, "symbolic-ref", "--quiet", "HEAD")
    # Nest task worktrees in the primary checkout, so removing a harness worktree cannot delete them.
    primary = _repo_root(root, linked_only=False)
    # An interrupted start leaves a detached task worktree; harness worktrees elsewhere may start.
    branch = current.stdout.strip().removeprefix("refs/heads/")
    if branch.startswith("lz/") and root == primary:
        raise CliError(f"primary checkout {root} is on task branch {branch}; switch it to another branch first")
    if branch.startswith("lz/") or (root != primary and root.parent.name == ".worktrees"):
        raise CliError(f"already inside task worktree {root}; run start from {primary}")
    config = config_mod.load(args.config or root / "workflow.toml")
    print(worktree.start(primary, args.slug, config.base_branch))
    return 0


def _check_failure_preview(tail: str) -> str:
    limit = 1536
    if len(tail.encode()) <= limit:
        return tail
    edge = limit // 8
    preview = b"\n".join(
        line if len(line) <= 2 * edge else line[:edge] + b" [...] " + line[-edge:]
        for line in (line.encode() for line in tail.splitlines())
    )
    return (preview if len(preview) <= limit else b"[...]\n" + preview[6 - limit:]).decode(errors="ignore")


def cmd_check(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    worktree.local_state_files(wt, CHECKS_FILE.name)
    if not config.checks:
        raise config_mod.ConfigError("checks.commands must list at least one command to run checks")
    before = _receipt_inode(wt / CHECKS_FILE)
    with worktree.lock_local_state(wt) as fd:
        if (_receipt_inode(wt / CHECKS_FILE) != before
                and (report := _current_check_report(wt, config, worktree.head(wt))) is not None
                and not worktree.is_dirty(wt) and sandbox.check_inputs_match(config, wt, report)):
            _print_receipt(wt, report, "reused")
            print("PASS")
            return 0
        report = sandbox.run_checks(
            config, wt, progress=lambda command: print(f"running: {github.redact_credentials(command)}", flush=True),
            status=lambda message: print(message, flush=True),
            checkpoint=partial(_save_report, wt, checkpoint_checks=config.checks, locked_fd=fd),
            workflow=args.config or f"origin/{config.base_branch}:workflow.toml",
        )
        return _finish_check(wt, config, report, fd)


def _receipt_inode(path: Path) -> int | None:
    with suppress(FileNotFoundError):
        return path.lstat().st_ino
    return None


def _finish_check(wt: Path, config: Config, report: CheckReport, locked_fd: int) -> int:
    shortened = False
    for result in (r for r in report.results if r.exit_code != 0):
        print(f"--- {github.redact_credentials(result.command)} (exit {result.exit_code}) ---", file=sys.stderr)
        tail = _proc.tail(result.tail)
        shortened |= (preview := _check_failure_preview(tail)) != tail
        print(f"{preview}\nFix the failed stage, then rerun `loopzero check`.", file=sys.stderr)
        if not config.network and (result.command.startswith("uv sync --offline --locked")
                                   or OFFLINE_FAILURE_RE.search(tail)):
            env = sandbox.check_environment(config)
            print(
                "\nThe sandbox has no network because `[checks] network = false`; "
                f"the effective UV_CACHE_DIR inside the sandbox is {env['UV_CACHE_DIR']}. "
                f"On origin/{config.base_branch}, set `[checks] env` UV_CACHE_DIR to a host cache "
                "and list it in `[checks] writable`. Warm that same cache with "
                "UV_CACHE_DIR=<path> uv sync --group dev on the host (use the required groups).", file=sys.stderr,
            )
    if (current_head := _save_report(wt, report, locked_fd=locked_fd)) is not None:
        print(f"check result discarded: checked HEAD {report.head}, current HEAD {current_head}; rerun loopzero check",
              file=sys.stderr)
        return 1
    fcntl.flock(locked_fd, fcntl.LOCK_UN)
    if shortened:
        print(f"Full diagnostic tail: {CHECKS_FILE} (head {report.head})", file=sys.stderr)
    if report.ok and report.dirty:
        print("note: dirty worktree; this check cannot satisfy loopzero ready", file=sys.stderr)
    elif report.ok:
        _print_receipt(wt, report, "produced")
    _refresh_pr_checks(wt, config)
    print("PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


def cmd_pr(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    _require_clean(wt)
    task = worktree.task_text(wt).replace("\r\n", "\n")
    _require_pr_details(wt, task)
    pr = github.merged_pr_for_branch(config.repo, branch, base=config.base_branch)
    if pr is not None and not (
        pr.head_sha != head and github.remote_branch_sha(config.repo, branch) != pr.head_sha
        and (landed := github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha, expected_base=config.base_branch))
        and _is_ancestor(wt, landed, worktree.base_sha(wt))
    ):
        raise CliError(f"task branch {branch} already merged as PR #{pr.number} ({pr.url}); "
                       "run `loopzero start <new-task-slug>` from the main checkout")
    pr = github.pr_for_branch(config.repo, branch, open_only=True)
    created = pr is None
    existing = _sync_task_narrative(task, pr.body if pr else "")
    _pr_body(wt, head, config, _seed_task_evidence(task, existing))  # refuse an unusable body before pushing
    force_lease = False
    git_branch = os.fsdecode(branch.encode("utf-8", "surrogateescape"))
    push = ["push", "origin"]
    if pr is not None:
        if config_mod.run_git(wt, "cat-file", "-e", f"{pr.head_sha}^{{commit}}").exit_code != 0:
            raise CliError(f"PR head {pr.head_sha[:12]} is not in this worktree; fetch and integrate it first")
        force_lease = pr.head_sha != head and not _is_ancestor(wt, pr.head_sha, head)
        if force_lease:
            previous_heads = _git(wt, "reflog", "show", "--format=%H", f"refs/heads/{git_branch}").splitlines()
            if not any(_is_ancestor(wt, pr.head_sha, previous) for previous in previous_heads):
                raise CliError(f"PR head {pr.head_sha[:12]} is absent from this task branch's history; integrate it first")
            push.extend(("--porcelain", f"--force-with-lease={git_branch}:{pr.head_sha}"))
    push.append(f"{head}:refs/heads/{git_branch}")  # the captured commit, never the mutable local name
    _require_push_target(wt, config.repo)
    _git_push(wt, push, force_lease=force_lease)
    _git(wt, "config", f"branch.{git_branch}.remote", "origin")
    _git(wt, "config", f"branch.{git_branch}.merge", f"refs/heads/{git_branch}")
    if pr is None:
        pr = github.create_draft_pr(config.repo, branch, config.base_branch, _pr_title(wt, branch), existing)
    with github.pr_lock(config.repo, pr.number):
        live = github.pr_view(config.repo, pr.number)
        if live.state != "OPEN":
            raise CliError(f"PR #{pr.number} closed during publication; reopen it before retrying loopzero pr")
        if (live.head_sha, live.base_ref) != (head, config.base_branch):
            raise CliError(f"PR #{pr.number} is at {live.head_sha[:12]} on {live.base_ref}; rerun loopzero pr")
        live_body = live.body.replace("\r\n", "\n")
        body = live_body if created else _sync_task_narrative(task, live_body)
        if (body := _pr_body(wt, head, config, _seed_task_evidence(task, body))) != live_body:
            github.update_body(config.repo, pr.number, body)
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


def _require_review_pr(config: Config, branch: str, head: str, *, repost: bool) -> github.PR:
    try:
        return _require_pr(config, branch, head_sha=head)
    except github.GhError as exc:
        if "HTTP 404" not in exc.tail or exc.command[:2] != ("gh", "api"):
            raise
        endpoint = exc.command[2]
        pulls = f"repos/{config.repo}/pulls"
        retry = "loopzero review --repost" if repost else "loopzero review"
        number = endpoint.removeprefix(f"{pulls}/")
        if endpoint.startswith(f"{pulls}/") and number.isdecimal():
            raise CliError(f"Could not read PR #{number}. Verify that PR and its branch {branch!r}, "
                           f"then rerun `{retry}`. {exc}") from exc
        if exc.tail.endswith(github.PR_APP_INSTALLATION_HINT):
            raise CliError(f"{exc}\nThen rerun `{retry}`.") from exc
        raise


def cmd_review(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    git_dir = _git(wt, "rev-parse", "--absolute-git-dir", paths=True)
    with ExitStack() as cleanup:
        fd = os.open(git_dir, os.O_RDONLY | os.O_DIRECTORY)
        cleanup.callback(os.close, fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CliError("review already running in this worktree") from exc
        return _review_locked(args, wt, config)


def _review_locked(args: argparse.Namespace, wt: Path, config: Config) -> int:  # noqa: C901 - PR guard, publish, repost and recovery paths
    runtime_source = ("reviewer paths are unpinned because --config was provided" if args.config
                      else f"reviewer paths pinned to origin/{config.base_branch}@{config.trusted_base_revision}")
    branch, head = worktree.branch(wt), worktree.head(wt)
    _require_task_branch(config, branch)
    kinds = ("primary", "delta")
    _, *saved_files = worktree.local_state_files(
        wt, "task.md", *(_review_file(wt, head, kind).name for kind in kinds)
    )
    _require_clean(wt)
    pr = _require_review_pr(config, branch, head, repost=args.repost)
    _require_pushed(pr, head)
    if pr.base_ref != config.base_branch:
        raise CliError(f"PR targets {pr.base_ref}, configured base is {config.base_branch}. Retarget the PR on GitHub and retry.")
    token = github.app_token()  # one identity for the publisher check and the post
    publisher = github.login(token=token)
    if config.review_publishers and publisher.casefold() not in {
        login.casefold() for login in config.review_publishers
    }:
        helper = (
            f" Restore the App token helper {github.APP_TOKEN_HELPER} as an executable that supplies "
            "a fresh installation token, then rerun `loopzero review`."
            if not token else " Use a configured publisher, then rerun `loopzero review`."
        )
        raise CliError(f"GitHub login {publisher!r} is not a configured review publisher. "
                       f"Configured publishers: {', '.join(config.review_publishers)}.{helper}")
    saved = [kind for kind, exists in zip(kinds, saved_files, strict=True) if exists] if args.repost else []
    if len(saved) > 1:
        raise CliError("multiple saved reviews for HEAD make publication ambiguous. Check the PR review history, then run `loopzero review` without `--repost`.")
    reviews = [] if saved else _pr_reviews(config.repo, pr.number)
    kind, reviewed = (saved[0], None) if saved else _select_review_round(config, pr, head, reviews)
    publication_context = {"repo": config.repo, "number": pr.number, "publisher": publisher}
    if args.repost:
        result, publication = _load_review(wt, head, kind)
        candidates, excluded = _repost_reviewer_families(wt, config, head, result)
        if publication is not None:
            if (not isinstance(publication, dict) or publication.get("repo") != config.repo
                    or publication.get("number") != pr.number
                    or not isinstance(publication.get("publisher"), str)
                    or not publication["publisher"]):
                raise CliError("saved publication identity differs from this repository/PR. Switch to the original PR worktree or run `loopzero review` without `--repost`.")
            confirmed, reviews = _recover_review(wt, config, pr, result,
                                                       publication["publisher"], token)
            if confirmed:
                return _review_verdict(result)
        else:
            reviews = _pr_reviews(config.repo, pr.number)
        # Exact read-back above may confirm an older published note; a new post needs the current form.
        if result.fallback and not _published_fallback(result.fallback, candidates, excluded):
            raise CliError("saved fallback note is not in the published form. Run `loopzero review` without `--repost`.")
        allowed, reviewed = _select_review_round(config, pr, head, reviews)
        if allowed != kind:
            raise CliError("saved review kind differs from the remaining review budget. Run `loopzero review` without `--repost`.")
        _delta_patch(wt, config, reviewed, head, pr.number)
    else:
        if args.model is None and args.effort is None and _has_recoverable_review(
            wt, config, head, kind, publication_context,
        ):
            raise CliError(f"saved {kind} review for {head[:12]}; run `loopzero review --repost`")
        if problem := _check_report_problem(wt, report := _load_report(wt), config, head, require_pass=False):
            raise CliError(problem)
        if report is not None and not report.ok:
            required = tuple(name for name in config.required_ci if name != eligibility.CONTEXT)
            checks = github._check_signals(config.repo, head, required_ci=required)[0] if required else {}
            blocked = [f"{name} ({checks.get(name, 'missing')})" for name in sorted(required) if checks.get(name) != "success"]
            if blocked or not required:
                raise CliError(f"hosted checks must pass on this head or rerun `loopzero check`: {', '.join(blocked) or 'no required hosted checks before review'}")
        result = _run_review(wt, config, head, kind, reviewed, args.model, args.effort,
                             number=pr.number, publisher=publisher, runtime_source=runtime_source)
        final_head, final_dirty = worktree.head(wt), worktree.is_dirty(wt)
        if final_head != head or final_dirty:
            raise CliError(
                f"worktree changed during review: HEAD before {head}, HEAD after {final_head}; "
                f"dirty after {final_dirty}; finish the changes, then run loopzero check and review again"
            )
    try:
        if not _matches_publication(result, publication_context):
            raise CliError("native publication context changed. To recover, run loopzero review from the intended PR worktree with its configured publisher.")
    except runners.RunnerBadOutput:
        _save_review(wt, result)
        raise
    prefix = _repost_prefix(result) if args.repost else review_marker(head, kind, "model")
    path = _review_file(wt, head, kind)
    _save_review(wt, result, publication_context)
    with github.pr_lock(config.repo, pr.number):
        try:  # a refused token or a GitHub error here leaves the saved review to --repost
            token = _refresh_publisher_token(token, publisher)
            _require_open_review_round(config, pr, head, kind)
        except (github.GhError, _proc.ProcTimeout, CliError) as exc:
            raise CliError(f"{exc} | review saved at {path}; run `loopzero review --repost`") from exc
        try:
            github.post_review(
                config.repo, pr.number, head, result, body_prefix=prefix, pr=pr, token=token,
                publisher=publisher, base=config.base_branch,
            )
        except (github.GhError, _proc.ProcTimeout) as exc:
            try:
                if _recover_review(wt, config, pr, result, publisher, token)[0]:
                    return _review_verdict(result)
            except (github.GhError, CliError, _proc.ProcTimeout) as recovery:
                raise CliError(f"{exc} | publication read-back blocked: {recovery}; "
                               f"review saved at {path}; retry `loopzero review --repost` "
                               "after resolving the read-back failure") from recovery
            retry = ("inspect the cause and run `loopzero review --repost` to reconcile or explicitly retry"
                     if isinstance(exc, (github.GhUnavailable, _proc.ProcTimeout)) else "GitHub rejected it and "
                     f"`--repost` resends the same review: fix the cause, or delete {path} and run `loopzero review`")
            raise CliError(f"{exc} | publication unconfirmed; review saved at {path}; {retry}") from exc
        try:
            live = github.pr_view(config.repo, pr.number)
            body = _append_review(live.body, result)
            with suppress(LoopZeroError, OSError, ValueError):
                refreshed = _pr_body(wt, head, config, live.body)
                if live.head_sha == head and "(no `loopzero check` run recorded)" not in refreshed:
                    body = _validate_pr_body_size(_append_review(refreshed, result))
            if (live.head_sha, live.state, live.base_ref) == (head, "OPEN", config.base_branch):
                github.update_body(config.repo, pr.number, body)
        except (github.GhError, _proc.ProcTimeout, OSError, ValueError) as exc:
            print(f"warning: could not append PR review summary: {_one_line(exc)}", file=sys.stderr)
    return _review_verdict(result)


def _require_open_review_round(config: Config, pr: github.PR, head: str, kind: str) -> None:
    pr = github.pr_view(config.repo, pr.number)
    if pr.head_sha != head:
        raise CliError("PR head moved during the review; pull it and rerun `loopzero review`")
    if pr.state != "OPEN" or pr.base_ref != config.base_branch:
        raise CliError(f"PR is {pr.state.lower()} targeting {pr.base_ref}, not open against {config.base_branch}. Reopen or retarget it on GitHub, then run `loopzero review --repost`.")
    reviews = _pr_reviews(config.repo, pr.number)
    markers = _pr_markers(reviews, head, config.review_publishers, repo=config.repo, number=pr.number)
    if any(marker.kind == kind and marker.head == head for marker in markers):
        raise CliError(f"the {kind} review round is already taken; run loopzero ready")
    if _select_review_round(config, pr, head, reviews)[0] != kind:
        raise CliError("review round changed during publication; run loopzero review again")


def _refresh_publisher_token(token: str, publisher: str) -> str:
    """The review may outlive the app token: re-mint it, but only for the same publisher."""
    if not token or (fresh := github.app_token()) == token:
        return token
    if not fresh or github.login(token=fresh).casefold() != publisher.casefold():
        raise CliError(f"could not refresh the app token for review publisher {publisher}; the review "
                       "is saved: fix the app token helper, then run `loopzero review --repost`")
    return fresh


def _review_verdict(result: ReviewResult) -> int:
    summary = ", ".join(f"{sum(f.severity == s for f in result.findings)} {s}" for s in runners.SEVERITIES)
    print(f"{result.kind} review by {result.family} on {result.head[:12]}: {result.verdict} ({summary})", end="\n" if result.verdict == "approve" else ". Inspect the PR review and run `loopzero resolve` to list open findings.\n")
    return 0 if result.verdict == "approve" else 5  # the exit code is the verdict, as for check


def _repost_reviewer_families(
    wt: Path, config: Config, head: str, result: ReviewResult,
) -> tuple[list[str], list[str]]:
    authors = runners.author_families(wt, head, _primary_base(wt, config))
    candidates = [family for family in config.reviewers if family not in authors]
    excluded = [family for family in config.reviewers if family in authors]
    if result.fallback and result.family not in excluded:
        raise CliError(f"saved review claims a same-family fallback, but {result.family} is not a configured "
                       "reviewer family that contributed to this PR lineage. Run `loopzero review` without `--repost`.")
    if not result.fallback and not candidates:
        raise IndependentReviewerUnavailable(
            "saved review has no same-family fallback disclosure, but every configured family "
            "contributed to this PR lineage; run without --repost to obtain a disclosed "
            f"same-family review. {_REPOST_ESCALATION}"
        )
    if result.family in authors and not result.fallback:
        raise IndependentReviewerUnavailable(
            f"saved reviewer family {result.family} contributed to this PR lineage; "
            f"run without --repost to obtain an independent review. {_REPOST_ESCALATION}"
        )
    return candidates, excluded


def _matches_publication(result: ReviewResult, publication: dict) -> bool:
    evidence = runners.codex_evidence(result)
    return evidence is None or (evidence["repo"], evidence["pr"], evidence["publisher"]) == (
        publication["repo"], publication["number"], publication["publisher"]
    )


def _has_recoverable_review(
    wt: Path, config: Config, head: str, kind: str, publication: dict,
) -> bool:
    if not _review_file(wt, head, kind).exists() or _review_file(wt, head, "delta" if kind == "primary" else "primary").exists():
        return False
    try:
        result, _ = _load_review(wt, head, kind, publication=publication)
        candidates, excluded = _repost_reviewer_families(wt, config, head, result)
        return (not result.fallback or _published_fallback(result.fallback, candidates, excluded)) and _matches_publication(result, publication)
    except (CliError, runners.RunnerBadOutput):
        return False


def _repost_prefix(result: ReviewResult) -> str:
    sessions = result.provenance["session_ids"]
    ids = ", ".join(f"`{session}`" for session in sessions)
    return f"{review_marker(result.head, result.kind, 'repost')}\nReposted from runner session{'' if len(sessions) == 1 else 's'}: {ids}"


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
    if result.family == "claude" and not old_settings and "configured_settings" not in result.provenance:
        formats.append(("requested-reported", True))
    if codex is not None and all("configured" not in call for call in codex["calls"]):
        formats.append(("requested-reported", False))
    return [(prefix, settings, display)
            for prefix in (review_marker(result.head, result.kind, "model"), _repost_prefix(result))
            for settings, display in formats]


def _comparable_body(body: str) -> str:
    """Normalize retired Codex inspection fields and the old notice heading, as eligibility reads them."""
    rest = body.removeprefix(github.BODY_BLOCKERS)
    notice = re.sub(r"<summary>Raw reviewer output(</summary>\n\n```\nNative Codex transcript retained in the "
                    r"restricted local review receipt; public evidence is publisher-derived, not backend attestation\. "
                    r"Exact raw SHA256: [0-9a-f]{64}\n```\n</details>)\Z",
                    r"<summary>Review evidence notice\1", rest)
    return body[:len(body) - len(rest)] + runners.retire_inspection(notice)


def _confirm_review(wt: Path, config: Config, pr: github.PR, result: ReviewResult,
                    publisher: str, reviews: list[dict]) -> bool:
    """Confirm an exact live publication; absence authorizes no automatic write."""
    trusted = config.review_publishers or (publisher,)
    # A publication starts with its marker; a review merely quoting the marker text is not one.
    evidence = [r for r in reviews if (marker := eligibility.leading_marker(r.get("body") or ""))
                and result.head in (r.get("commit_id"), marker.group(1))]
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
        expected = next((p for p in payloads if p["body"] == _comparable_body(review.get("body") or "")), None)
        review_id = review.get("id")
        if (expected is None or review.get("commit_id") != result.head or review.get("state") not in states
                or eligibility.review_login(review) != publisher.casefold()
                or type(review_id) is not int or review_id <= 0):
            hint = ("; for a publication format mismatch, use the publishing CLI revision or upgrade the recovery CLI"
                    if expected is None else "")
            raise CliError(f"conflicting or ambiguous review publication{hint}")
        comments = github.paged(f"repos/{config.repo}/pulls/{pr.number}/reviews/{review_id}/comments")
        if (any(c.get("start_line") is not None or c.get("start_side") is not None
                or c.get("commit_id") != result.head or c.get("pull_request_review_id") != review_id
                or eligibility.review_login(c) != publisher.casefold()
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


def _delta_patch(wt: Path, config: Config, since: str | None, head: str, number: int) -> bytes:
    if since is None:
        return b""
    if not _is_ancestor(wt, since, head):
        raise CliError(
            f"reviewed head {since[:12]} is not an ancestor of this branch. Find the original tip with `git reflog`. "
            "Back up your branch, restore that tip and replay later fixes. Fetch the base and "
            f"merge `{shlex.quote(f'origin/{config.base_branch}')}` into the restored branch, then run `loopzero check`, `loopzero pr` and `loopzero review`, or open a new PR."
        )
    merged = bool(worktree.git(wt, "rev-list", "--first-parent", "--reverse", "--merges", f"{since}..HEAD"))
    if not merged:
        patch = worktree.diff_since(wt, since)  # Empty after an empty commit or a reverted change.
    else:
        patch = eligibility.base_merge_material(config.repo, number, since, head, config.base_branch, clean=False)
        if patch is None:
            raise CliError(
                f"could not verify merge history: {since[:12]} must be on the first-parent history and "
                f"merged parents must come from {config.base_branch}. Check the first-parent log and merged "
                "parents. If the history is correct, GitHub objects may be unavailable or exceed verification "
                "limits. Open a new PR from the current base for a fresh review if verification still fails."
            )
    if not patch:
        publishers = config.review_publishers or (github.login(),)
        if github.open_blocking_findings(config.repo, number, None, publishers):
            raise CliError("empty delta leaves blocking findings open. Run `loopzero resolve` "
                           "to list them, then address them before readiness.")
        prior = eligibility.eligible_review(_pr_reviews(config.repo, number), since, publishers,
                                            repo=config.repo, number=number)
        if eligibility.body_blocker(prior):
            raise CliError("empty delta leaves blocking findings in the review body. Push fixes before another review.")
        if merged and eligibility.base_merge_material(
            config.repo, number, since, head, config.base_branch, clean=True,
        ) is not None:
            raise CliError(f"nothing to review since {since[:12]}; run `loopzero ready` or push the fixes first")
    return patch


def _run_review(  # noqa: C901 - reviewer fallback across families
    wt: Path, config: Config, head: str, kind: str, reviewed: str | None,
    model: str | None = None, effort: str | None = None, *, number: int, publisher: str, runtime_source: str = "",
) -> ReviewResult:
    lineage_base = _primary_base(wt, config)
    since = reviewed if kind == "delta" else lineage_base
    patch = (_delta_patch(wt, config, since, head, number)
             if kind == "delta" else worktree.diff_since(wt, since))
    diff = runners.render_patch(patch)
    task = worktree.task_text(wt)
    authors = runners.author_families(wt, head, lineage_base)
    candidates = [family for family in config.reviewers if family not in authors]
    errors: dict[str, tuple[str, str]] = {}
    excluded = [family for family in config.reviewers if family in authors]
    review = partial(runners.review_with, cwd=wt, head=head, kind=kind, task_text=task,
                     reviewer_ro_paths=config.reviewer_ro_paths)
    attempts = candidates + excluded
    selected: dict[str, Callable[..., ReviewResult] | CliError] = {}
    for family in attempts:
        settings = config.review.get(family, ReviewConfig())
        chosen = effort or settings.effort
        if chosen and settings.allowed_efforts and chosen not in settings.allowed_efforts:
            problem = CliError(f"effort {chosen!r} is not allowed for {family}; expected one of {settings.allowed_efforts}")
            if candidates and family == candidates[0]:
                raise problem
            selected[family] = problem
            continue
        context = ({"repo": config.repo, "pr": number, "publisher": publisher, "head": head, "kind": kind,
                    "base": since, "diff_sha256": hashlib.sha256(patch).hexdigest(), "diff_bytes": len(patch)}
                   if family == "codex" else None)
        selected[family] = partial(review, family, model=model or settings.model, effort=chosen, review_context=context)
    if runtime_source:
        print(runtime_source)
    fallback = ""
    reasons: list[str] = []  # published; runner detail stays in local stderr and failures
    for index, family in enumerate(attempts):
        launch = selected[family]
        if isinstance(launch, CliError):
            errors[family] = str(launch), ""
            reasons.append(f"{family}: unavailable")
            continue
        if family in excluded and not fallback:
            fallback = ("independent reviewers failed: " + "; ".join(reasons)
                        if candidates else "every configured family contributed (" + ", ".join(excluded) + ")")
            print(f"same-family fallback: {fallback}", file=sys.stderr)
        try:
            try:
                result = launch(diff=diff)
            except runners.RunnerPromptTooLong:
                chunks = runners.split_diff(diff)
                print(f"reviewer {family} rejected the full diff; reviewing {len(chunks)} chunks", file=sys.stderr)
                chunk_results = [launch(diff=chunk, chunk=(index, len(chunks)))
                                 for index, chunk in enumerate(chunks, 1)]
                result = runners.merge_reviews(chunk_results)
            break
        except RUNNER_FAILURES as exc:
            reason, detail, fallback_reason = _review_failure(family, exc)
            errors[family] = detail, exc.tail if isinstance(exc, runners.RunnerMalformedOutput) else ""
            reasons.append(f"{family}: {fallback_reason}")
            if any(not isinstance(selected[name], CliError) for name in attempts[index + 1:]):
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
    # The cause of a failed reviewer is at the end of its output tail.
    detail = "\n".join(lines[:1] + ([] if isinstance(exc, runners.RunnerMalformedOutput) else lines[1:][-2:]))
    if isinstance(exc, runners.RunnerAuthFailed):
        login = "claude auth login" if family == "claude" else "codex login"
        fix = f"run `{login}`"
        if family == "claude" and (names := [name for name in runners.CLAUDE_AUTH_ENV if os.environ.get(name)]):
            fix = "replace or unset " + " and ".join(f"`{name}`" for name in names)
        return lines[0], detail + f"\nFix: {fix}.", _FALLBACK_REASONS[0]
    if isinstance(exc, runners.RunnerUsageLimit):
        return lines[0], detail + "\nFix: wait for the limit to reset or switch this reviewer's account.", _FALLBACK_REASONS[1]
    if isinstance(exc, runners.RunnerMissing):
        package = "@anthropic-ai/claude-code" if family == "claude" else "@openai/codex"
        fix = f"install it with `npm install -g {package}`"
        if family == "codex":
            fix += " and list the npm prefix in `[delivery] reviewer_ro_paths` on the delivery branch"
        return lines[0], detail + f"\nFix: {fix}.", _FALLBACK_REASONS[2]
    return lines[0], detail, _FALLBACK_REASONS[3]


_REPOST_ESCALATION = (
    "If that fresh review cannot run, restore a configured reviewer and retry, or report "
    "the blocker to whoever commissioned this PR."
)


def _independence_message(
    authors: set[str], excluded: list[str], failures: list[str]
) -> str:
    identity = ", ".join(sorted(authors)) or "unknown"
    excluded_text = ", ".join(excluded) if excluded else "none"
    detail = ("\n\nFailures:\n" + "\n\n".join(failures)) if failures else ""
    return (
        f"no configured reviewer can run (author family: {identity}; excluded families: "
        f"{excluded_text}). Fix the reviewer failures below and rerun. If you cannot, "
        f"report the blocker to whoever commissioned this PR. "
        f"Do not remove truthful Co-authored-by attribution.{detail}"
    )


def _review_file(wt: Path, head: str, kind: str) -> Path:
    return wt / ".loopzero" / f"review-{head[:12]}-{kind}.json"


def _save_review(wt: Path, result: ReviewResult, publication: dict | None = None) -> None:
    data = dataclasses.asdict(result) | ({"publication": publication} if publication is not None else {})
    worktree.write_local_state(wt, _review_file(wt, result.head, result.kind).name, json.dumps(data, indent=2) + "\n", private=True)


def _load_review(
    wt: Path, head: str, kind: str, *, publication: dict | None = None,
) -> tuple[ReviewResult, object]:
    path = _review_file(wt, head, kind)
    try:
        data = json.loads(worktree.read_local_state(wt, path.name))
        if not isinstance(data, dict):
            raise ValueError("review receipt must be an object")
        if data.get("head") != head:
            raise CliError(f"{path} holds a review of {str(data.get('head'))[:12]}, not HEAD {head[:12]}. Run `loopzero review` without `--repost`.")
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
        raise CliError(f"no saved {kind} review for {head[:12]} at {path}. Run `loopzero review` without `--repost`.") from None
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise CliError(f"{path} is not runner-produced: invalid saved review. Run `loopzero review` without `--repost`.") from exc
    if not runners.validate_runner_result(result):
        raise CliError(f"{path} is not runner-produced: provenance/envelope mismatch. Run `loopzero review` without `--repost`.")
    return result, saved_publication


def _pr_readiness(
    wt: Path, config: Config, branch: str | None = None, *, allow_merged: bool = False,
    workflow_wait: bool = False, mergify_deadline: float | None = None,
) -> tuple[github.PR, github.Readiness | None]:
    with github.bounded_reads(mergify_deadline, token=None, retry=True) if mergify_deadline is not None else nullcontext():
        branch = worktree.branch(wt) if branch is None else branch
        head = worktree.head(wt)
        _require_task_branch(config, branch)
        pr = _require_pr(config, branch, allow_finished=allow_merged, head_sha=head)
        _require_pushed(pr, head)
        return pr, None if pr.state != "OPEN" else _readiness(
            wt, config, pr, head, workflow_wait=workflow_wait, announce_receipt=True, mergify_deadline=mergify_deadline,
        )


CLEANUP_RETRY = "; fix the cause, then rerun `loopzero merge` in this worktree to finish cleanup"


def _cleanup(wt: Path, pr: github.PR, *, closed: bool = False) -> None:
    outcome = "closed" if closed else "merged"
    name = None
    try:
        root = _repo_root(wt)
        name = worktree.branch(wt)
        worktree.require_started(wt, root)
        worktree.cleanup(wt, pr.head_sha, keep_branch=closed)
        if closed:
            print(f"removed worktree {wt}; kept both branch refs", file=sys.stderr)
    except PrimaryCheckout as exc:
        print(f"note: {outcome}; {exc}", file=sys.stderr)
    except (LoopZeroError, OSError) as exc:
        retry = CLEANUP_RETRY if wt.exists() else ""
        if not retry and name is not None:
            retry = f"; worktree removed; delete local branch {name!r} if it still points at the merged head"
        print(f"warning: {outcome} but worktree cleanup failed: {exc}{retry}", file=sys.stderr)


OWNER_ROUTE = "If the delivery contract's owner queue route applies, post its one reason on the PR before taking it out of draft."


def _recheck_activation(wt: Path, config: Config, live: github.PR, args: argparse.Namespace,
                        deadline: float | None, *, retry: bool) -> github.PR:
    _verify_wait_snapshot(args, wt, config, live.head_sha, live.head_sha, config)
    fresh = _readiness(wt, config, live, live.head_sha, workflow_wait=True, mergify_deadline=deadline)
    if (retry and live.mergeable == "UNKNOWN") or not (fresh.ready or _draft_checks_waiting(config, live, fresh)):
        _require_receipt(wt, config, live.head_sha, suffix=f". {OWNER_ROUTE}")
        raise CliError(f"draft activation blocked: {'; '.join(fresh.reasons)}; rerun loopzero ready")
    latest = github.pr_view(config.repo, live.number)
    if ((latest.head_sha, latest.state, latest.base_ref) != (live.head_sha, live.state, live.base_ref)
            or github.merge_hold(latest.number, latest.labels)
            or (latest.is_draft and (latest.mergeable not in (live.mergeable, "MERGEABLE")
                                     or (latest.merge_state == "BEHIND" and live.merge_state != "BEHIND")))):
        raise CliError("PR changed during draft activation; rerun loopzero ready")
    return latest


def _mark_ready(
    wt: Path, config: Config, number: int, head: str, args: argparse.Namespace, deadline: float | None,
) -> None:
    with (github.bounded_reads(deadline, token=None, retry=True) if deadline is not None else nullcontext()), github.pr_lock(config.repo, number):
        observe_only = None
        for retry in (False, True):
            live = github.pr_view(config.repo, number)
            github.read_timeout(0)
            seen = (live.head_sha, live.state, live.base_ref)
            if problem := (github.merge_hold(number, live.labels)
                           or (seen != (head, "OPEN", config.base_branch) and f"PR changed to {seen}")
                           or (live.mergeable == "CONFLICTING" and github.mergeability_blocker(live))):
                raise CliError(f"{problem}; rerun loopzero ready")
            if not live.is_draft:
                return
            if observe_only is not None:
                raise observe_only
            if not (live := _recheck_activation(wt, config, live, args, deadline, retry=retry)).is_draft:
                return
            _require_receipt(wt, config, head, suffix=f". {OWNER_ROUTE}")
            try:
                github.mark_ready(config.repo, number, live.node_id)
                return
            except (github.GhUnavailable, _proc.ProcTimeout) as exc:
                remaining = github.read_timeout()
                if retry or deadline is None or github._rate_limited(None, {}, str(exc)):
                    raise
                observe_only = exc if remaining <= github.RETRY_DELAY else None
                if observe_only is None:
                    _sleep(github.RETRY_DELAY)


def cmd_ready(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    started = time.monotonic()
    deadline = started + args.wait if args.wait else None
    with suppress(github.ReadDeadline):
        return _ready(args, wt, config, started, deadline)
    print(f"timed out while checking readiness. {_wait_resume(args)}")
    return 3


def _ready(  # noqa: C901 - wait and draft activation paths share one readiness flow
    args: argparse.Namespace, wt: Path, config: Config, started: float, deadline: float | None,
) -> int:
    pr, readiness = _pr_readiness(wt, config, workflow_wait=args.wait is not None, mergify_deadline=deadline)
    assert readiness is not None
    wait = partial(_wait_with_deadline, wt, config, timeout=args.wait, started=started, args=args,
                   allow_merged=True, allow_skipped=True, initial_read_time=time.monotonic() - started)
    waiting = _draft_checks_waiting(config, pr, readiness)
    if kicked := bool(waiting):
        _mark_ready(wt, config, pr.number, pr.head_sha, args, deadline)
        if args.wait is None:
            pending = f"; {github.MERGEABILITY_PENDING}" if pr.mergeable == "UNKNOWN" else ""
            print(f"marked ready; waiting for required checks: {', '.join(waiting)}{pending}; {NEXT_WAIT}")
            return 3
        print("marked ready; " + "; ".join(readiness.reasons))
    if args.wait is not None and (kicked or _pending_checks(readiness, allow_skipped=not pr.is_draft)):
        if (waited := wait(pr, kicked=kicked, shown=_by_check(readiness, readiness.reasons) if kicked else (),
                           initial=None if kicked else readiness)) is None:
            return 3
        pr, readiness, kicked = waited
    if readiness is not None and readiness.ready and pr.is_draft and not kicked:
        _mark_ready(wt, config, pr.number, pr.head_sha, args, deadline)
        if args.wait is not None:
            if (waited := wait(pr, kicked=True)) is None:
                return 3
            pr, readiness, _ = waited
    if readiness is None:
        print(f"merged: {pr.url}")
        return 0
    for reason in _readiness_messages(pr, readiness):
        print(f"not ready: {reason}")
    if not readiness.ready:
        if pr.is_draft and any(reason.startswith(("run loopzero check", "check receipt")) for reason in readiness.reasons):
            print(OWNER_ROUTE)
        if args.wait is None and _pending_checks(readiness):
            print(NEXT_WAIT)
            return 3
        return 1
    print(f"ready: {pr.url}")
    return 0


def _check_reasons(readiness: github.Readiness, *states: str) -> dict[str, str]:
    return {reason: name for reason, (name, state) in readiness.checks.items() if state in states}


def _draft_checks_waiting(config: Config, pr: github.PR, readiness: github.Readiness) -> tuple[str, ...]:
    reasons = set(readiness.reasons) - {github.MERGEABILITY_PENDING}
    if not pr.is_draft or not reasons or not reasons <= (expected := _check_reasons(readiness, "missing", "skipped")).keys():
        return ()
    return tuple(name for name in config.required_ci if name in {expected[reason] for reason in reasons})


def _pending_checks(readiness: github.Readiness, *, allow_skipped: bool = False) -> tuple[str, ...]:
    states = ["missing", "pending", *(["skipped"] if allow_skipped else [])]
    waitable = {*_check_reasons(readiness, *states), *readiness.waitable_failures, github.MERGEABILITY_PENDING}
    return readiness.reasons if all(reason in waitable for reason in readiness.reasons) else ()


def _readiness_messages(pr: github.PR, readiness: github.Readiness) -> Iterator[str]:
    for reason in readiness.reasons:
        _, state = readiness.checks.get(reason, ("", "missing"))
        if state not in {"missing", "capped", "pending", "skipped"} and reason not in readiness.waitable_failures:
            reason += (f". Inspect {pr.url}/checks. Fix reproducible failures before retrying. "
                       "Rerun the failed job only for a transient failure. Once green, run `loopzero ready --wait`.")
        yield reason


def _merge_check_hint(config: Config, pr: github.PR, readiness: github.Readiness) -> str:
    if _pending_checks(readiness):
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
        pause = max(WAIT_INTERVAL, delay * random.uniform(0.9, 1.0))
        if bounded and remaining <= pause:
            baseline = min(5.0, max(0.5, remaining / 10))
            allowance = min(remaining / 2, max(baseline, 2 * longest_read))
            _sleep(remaining - allowance)
            yield
            if (remaining := timeout - (time.monotonic() - started)) > 0:
                _sleep(remaining)
            return
        _sleep(min(remaining, pause))
        delay = min(120.0, delay * 2)


def _wait_with_deadline(
    wt: Path, config: Config, pr: github.PR, timeout: float, *, kicked: bool,
    started: float, shown: tuple = (), args: argparse.Namespace,
    allow_merged: bool = False, allow_skipped: bool = False, initial: github.Readiness | None = None,
    initial_read_time: float = 0.0,
) -> tuple[github.PR, github.Readiness | None, bool] | None:
    wait = partial(_wait_for_checks, wt, config, pr, timeout, kicked=kicked, started=started, shown=shown,
                   args=args, allow_merged=allow_merged, allow_skipped=allow_skipped, initial=initial)
    deadline = started + timeout
    try:
        with github.bounded_reads(deadline, token=None, retry=True, check_on_exit=False) if timeout else nullcontext():
            waited = wait(bounded=bool(timeout), initial_read_time=initial_read_time if timeout else 0)
            if timeout and waited is not None:
                github.read_timeout(0)
            return waited
    except (github.ReadDeadline, _proc.ProcTimeout) as exc:
        if not timeout or (isinstance(exc, _proc.ProcTimeout) and time.monotonic() < deadline):
            raise
    _print_check_wait_timeout(pr, args)
    return None


def _print_check_wait_timeout(pr: github.PR, args: argparse.Namespace) -> None:
    waiting_for = "GitHub to compute mergeability" if pr.mergeable == "UNKNOWN" else "required checks"
    detail = f"timed out waiting for {waiting_for} on {pr.head_sha[:12]}."
    print(detail if args.command == "merge" else f"{detail} {_wait_resume(args)}")


def _wait_resume(args: argparse.Namespace) -> str:
    config = [f"--config={args.config}"] if args.config else []
    return f"Run `{shlex.join(['loopzero', *config, args.command, '--wait'])}` to resume on this head."

def _verify_wait_snapshot(
    args: argparse.Namespace, wt: Path, config: Config, head: str, current_head: str,
    previous: Config,
) -> Config:
    if current_head != head:
        raise CliError(f"PR head changed while waiting: {head[:12]} to {current_head[:12]}; sync the branch and run loopzero ready")
    latest = _load_config(args, wt, previous=previous, refresh=True)
    if latest == config or (
        dataclasses.replace(latest, env_allowlist=config.env_allowlist) == config
        and (set(latest.env_allowlist) == set(config.env_allowlist)
             or sandbox.settings_fingerprint(latest) == sandbox.settings_fingerprint(config))
    ):
        return latest
    retry = "retry with the current settings" if _current_check_report(wt, latest, head) else "run loopzero check and retry"
    raise CliError(f"workflow settings changed while waiting; {retry}")


def _by_check(readiness: github.Readiness, reasons: tuple[str, ...]) -> tuple:
    return tuple(((r in readiness.checks, readiness.checks.get(r, (r,))[0]), r) for r in reasons)


def _print_wait_changes(shown: tuple, pending: tuple) -> None:
    previous, current = dict(shown), dict(pending)
    if changed := [reason for key, reason in current.items() if previous.get(key) != reason]:
        print("waiting: " + "; ".join(changed))
    if cleared := [key[1] for key in previous if key not in current]:
        print("cleared: " + "; ".join(cleared))


def _read_wait_poll[T](read: Callable[[], T], deadline: float) -> T:
    while True:
        try:
            return read()
        except github.GhUnavailable as exc:
            delay = (exc.rate_reset or 0) - time.time()
            if not 0 < delay < deadline - time.monotonic():
                raise
            print(f"GitHub quota exhausted. Waiting {delay:.0f}s for the verified reset.", flush=True)
            _sleep(delay)
            if time.monotonic() >= deadline:
                raise


def _wait_for_checks(
    wt: Path, config: Config, pr: github.PR, timeout: float, *, kicked: bool,
    started: float, args: argparse.Namespace, shown: tuple = (),
    bounded: bool = False, allow_merged: bool = False,
    allow_skipped: bool = False, initial: github.Readiness | None = None,
    initial_read_time: float = 0.0,
) -> tuple[github.PR, github.Readiness | None, bool] | None:
    grace_started = time.monotonic()
    current = pr
    stale_notice = False
    previous = config
    deadline = started + timeout

    def poll() -> tuple[github.PR, Config, github.Readiness | None]:
        observed = github.pr_view(config.repo, pr.number)
        latest = _verify_wait_snapshot(args, wt, config, pr.head_sha, observed.head_sha, previous)
        readiness = None if allow_merged and observed.state == "MERGED" else _readiness(
            wt, config, observed, pr.head_sha, workflow_wait=True, mergify_deadline=deadline if bounded else None,
        )
        return observed, latest, readiness

    for _ in _poll_until(started, timeout, bounded=bounded, initial_read_time=initial_read_time):
        readiness, initial = initial, None
        if readiness is None:
            current, previous, readiness = _read_wait_poll(poll, deadline)
        if readiness is None:
            return current, None, kicked
        if not kicked and _draft_checks_waiting(config, current, readiness):
            github.read_timeout(0, scoped=bounded)
            _mark_ready(wt, config, pr.number, current.head_sha, args, deadline if bounded else None)
            print("marked ready; " + "; ".join(readiness.reasons))
            return _wait_for_checks(wt, config, pr, timeout, kicked=True, started=started,
                                    shown=_by_check(readiness, readiness.reasons), args=args, bounded=bounded,
                                    allow_merged=allow_merged)
        pending = _pending_checks(readiness, allow_skipped=kicked or (allow_skipped and not current.is_draft))
        if pending or readiness.ready:
            _print_wait_changes(shown, shown := _by_check(readiness, pending))
        if not pending:
            return current, readiness, kicked
        elapsed = time.monotonic() - grace_started
        if readiness.stale_verdicts and not stale_notice:
            print("waiting: required check verdict predates the review; awaiting refresh: "
                  + ", ".join(readiness.stale_verdicts))
            stale_notice = True
        if (
            elapsed >= MISSING_RUN_GRACE
            and set(pending) <= _check_reasons(readiness, "missing", "skipped").keys()
            and not _read_wait_poll(partial(github.has_check_activity, config.repo, pr.head_sha, readiness), deadline)
        ):
            raise CliError(
                f"{'; '.join(pending)} after {elapsed:.0f}s: inspect workflow scheduling, "
                f"triggers and permissions for head {pr.head_sha[:12]}; after correcting the cause, "
                "rerun the same `--wait` command"
            )
    _print_check_wait_timeout(current, args)
    return None


def _queue_wait(deadline: float, poll: Callable[[float], str | None], *, bounded: bool) -> str | None:
    if not bounded:  # Explicit --wait=0 still observes once.
        return poll(0)
    with github.bounded_reads(deadline, token=None, retry=True, check_on_exit=False):
        try:
            return poll(github.read_timeout())
        except (github.MergeFailed, github.ClosedUnmerged, CliError):
            raise  # Concluded from reads that finished inside the budget.
        except LoopZeroError:
            github.read_timeout(0)
            raise


def _landed(
    config: Config, pr: github.PR, closed: list[github.GhError], *, confirm_closed: bool = False,
) -> str | None:
    try:
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha, expected_base=pr.base_ref)
    except github.ClosedUnmerged as exc:
        github.read_timeout(0)  # A late observation is a timeout, not evidence.
        confirmed = confirm_closed and bool(closed)
        closed[:] = [exc]
        if confirmed:
            raise
        return None
    closed.clear()  # Even a late read contradicts earlier lagging closed evidence.
    github.read_timeout(0)
    return sha


def _wait_for_merge(config: Config, pr: github.PR, timeout: float) -> str | None:
    started = time.monotonic()
    closed: list[github.GhError] = []
    print(f"PR #{pr.number} is queued for merge into {config.base_branch}; waiting")
    with suppress(github.ReadDeadline):
        for _ in _poll_until(started, timeout, bounded=True):
            if sha := _landed(config, pr, closed, confirm_closed=True):
                return sha
            if not closed and not github.merge_pending(config.repo, pr.number, pr.head_sha):
                if sha := _landed(config, pr, closed):
                    return sha
                if not closed:
                    raise CliError(f"PR #{pr.number} left the merge queue unmerged; inspect the PR before rerunning loopzero merge --wait")
    if closed:  # Recorded only from reads that finished inside the budget.
        raise closed[0]
    return None


def _merge_and_follow(
    config: Config, pr: github.PR, deadline: float | None, admit_by: float | None,
) -> str | None:
    merge = partial(github.merge, config.repo, pr.number, config.merge_strategy, pr.head_sha, config.base_branch)
    if deadline is None:
        return merge()
    with suppress(github.ReadDeadline):
        return _queue_wait(deadline, lambda remaining: merge() or _wait_for_merge(
            config, pr, remaining and max(0.0, deadline - time.monotonic())),
                           bounded=admit_by is not None)
    return None


def _mergify_timeout_detail(
    config: Config, pr: github.PR, queue: str, state: mergify.Membership | None,
) -> str:
    if state is None:
        report = None
        with suppress(github.GhError):
            github.read_timeout(0)
            report = mergify.queue_attempt(config.repo, pr.number, pr.head_sha, queue).admission
        return f"waiting for admission to Mergify queue {queue}. " + (
            f"Mergify report: {report}" if report else "Check the PR for Mergify's admission report."
        )
    location = (
        f"https://github.com/{config.repo}/pull/{state.candidate_number}"
        if state.candidate_number is not None else
        f"https://dashboard.mergify.com/orgs/{config.repo.replace('/', '/repos/', 1)}/queues/status"
        f"?branch={quote(config.base_branch, safe='')}&pull={pr.number}"
    )
    detail = f"still queued in {queue}. {location}"
    if state.unmet_conditions:
        shown = [label[:160].decode(errors="ignore") + ("…" if len(label) > 160 else "")
                 for label in (github._redact_tokens(condition).encode() for condition in state.unmet_conditions[:4])]
        detail += ". Unmet candidate conditions: " + "; ".join(shown)
        if omitted := len(state.unmet_conditions) - len(shown):
            detail += f"; {omitted} more"
    return detail


MERGIFY_REQUEUE = (
    "To requeue an unchanged head, post `@mergifyio queue` as the whole PR comment (same login); a new head is "
    "requested by `loopzero merge` or readmitted by a repository queue rule; Mergify's \"already queued\" reply "
    "to a request means a rule queued it first and is harmless. Then rerun `loopzero merge --wait`."
)
MERGIFY_DEQUEUE_HINT = 'Read Mergify\'s "Left the queue" comment on the PR for the reason. Address it before requeueing.'


def _mergify_departure(
    config: Config, pr: github.PR, queue: str, closed: list[github.GhError], confirmed: bool,
    report: mergify.Departure | None,
) -> str | None:
    if not confirmed and report is None:
        return None
    sha = _landed(config, pr, closed)
    if sha or closed:
        return sha
    detail, requeue = MERGIFY_DEQUEUE_HINT, MERGIFY_REQUEUE
    if report is not None:
        detail = f"Mergify report: {report.url}."
        if report.reason and report.failure_report is None:
            detail += f" Departure reason: {github._redact_tokens(report.reason)!r}."
        if report.attribution == "base_conflict":
            detail = (f"The PR conflicts with {pr.base_ref}. Run `git fetch origin {shlex.quote(pr.base_ref)}` and resolve the base "
                      f"integration according to the PR's review state. {detail}")
            requeue = ("After resolving the conflict, run `loopzero check`, `loopzero pr`, "
                       "`loopzero review`, `loopzero ready --wait` and `loopzero merge --wait`.")
        else:
            if report.checks:
                detail += " Failing checks: " + ", ".join(repr(github._redact_tokens(check)) for check in report.checks) + "."
            detail += " " + {
                "stacked": "Mergify tested a stack and could not attribute the failure. Inspect the candidate in the report or requeue after the earlier PRs clear.",
                "isolated": "Mergify tested this PR alone. Inspect or fix the failed check before requeueing unless it is a known flake.",
            }.get(report.attribution, MERGIFY_DEQUEUE_HINT) + report.jobs(config.repo)
    raise CliError(f"PR #{pr.number} left Mergify queue {queue} unmerged. {detail} {requeue}")


def _mergify_wait_poll(
    config: Config, pr: github.PR, queue: str, deadline: float | None, closed: list[github.GhError], confirmed: bool,
) -> tuple[str | None, mergify.Membership | None, bool]:
    sha = _landed(config, pr, closed, confirm_closed=True)
    if sha or closed:
        return sha, None, False
    if not mergify.configured(config.repo, config.base_branch, queue, deadline):
        raise CliError(
            "Mergify queue configuration became unsafe or unavailable while waiting. "
            f"Check queue {queue!r} for {config.base_branch!r} in `.mergify.yml` and the Mergify dashboard. "
            "Once the queue is reachable and preserves PR heads, rerun `loopzero merge --wait`."
        )
    state = mergify.membership(config.repo, pr.number, queue, deadline)
    deferred = deadline is not None and confirmed and state is not None
    sha = None if deferred else _landed(config, pr, closed)
    if state is None and sha is None:
        report = mergify.departure_report(config.repo, pr.number, pr.head_sha, queue)
        sha = _mergify_departure(config, pr, queue, closed, confirmed, report)
    return sha, state, deferred


def _wait_for_mergify(config: Config, pr: github.PR, timeout: float, confirmed: bool) -> str | None:
    started = time.monotonic()
    closed: list[github.GhError] = []
    queue = config.mergify_queue
    assert queue is not None
    state, deferred_landing_read = None, False
    print(f"PR #{pr.number} following Mergify queue {queue}; head {pr.head_sha[:12]}; waiting up to {timeout:g}s")
    polls = _poll_until(started, timeout, bounded=True, admitted=lambda: confirmed)
    deadline = started + timeout
    with suppress(github.ReadDeadline):
        for _ in polls:
            poll = partial(_mergify_wait_poll, config, pr, queue, deadline if timeout else None, closed, confirmed)
            sha, state, deferred_landing_read = _read_wait_poll(poll, deadline)
            if sha:
                return sha
            if state is not None and not confirmed:
                with github.pr_lock(config.repo, pr.number):
                    if not mergify.queue_attempt(config.repo, pr.number, pr.head_sha, queue).confirmed:
                        mergify.mark_confirmed(config.repo, pr.number, pr.head_sha, queue)
                confirmed = True
                print(f"PR #{pr.number} confirmed in Mergify queue {queue}; waiting")
        if deferred_landing_read and (sha := _landed(config, pr, closed)):
            return sha
    if closed:  # Recorded only from reads that finished inside the budget.
        raise closed[0]
    print(f"timed out waiting for Mergify to land PR #{pr.number}")
    print(_mergify_timeout_detail(config, pr, queue, state))
    return None


_FOLLOW_QUEUE = "next: loopzero merge --wait follows the queue, verifies and cleans up"


def _follow_blocked_mergify(
    config: Config, pr: github.PR, readiness: github.Readiness, queue: str, deadline: float | None, *, bounded: bool,
) -> str | None:
    hint = _merge_check_hint(config, pr, readiness)
    if deadline is not None and pr.base_ref == config.base_branch:
        try:
            state = mergify.membership(config.repo, pr.number, queue, deadline if bounded else None)
        except github.ReadDeadline:
            print(f"timed out waiting for Mergify to land PR #{pr.number}")
            return None
        except mergify.MergifyError as exc:
            hint += f"; Mergify membership unverified: {exc}"
        else:
            if state is not None:
                return _queue_wait(deadline, partial(_wait_for_mergify, config, pr, confirmed=True), bounded=bounded)
            report = mergify.departure_report(config.repo, pr.number, pr.head_sha, queue)
            if sha := _mergify_departure(config, pr, queue, [], confirmed=False, report=report):
                return sha
    raise CliError("not ready to request Mergify: " + "; ".join(_readiness_messages(pr, readiness)) + hint)


def _merge_with_mergify(
    wt: Path, config: Config, pr: github.PR,
    readiness: github.Readiness, deadline: float | None, admit_by: float | None = None,
) -> str | None:
    queue = config.mergify_queue
    assert queue is not None
    if not readiness.ready:
        return _follow_blocked_mergify(config, pr, readiness, queue, deadline, bounded=admit_by is not None)
    if pr.is_draft:
        raise CliError(f"PR #{pr.number} is still a draft; next: loopzero ready --wait")
    with worktree.lock_local_state(wt, wait=lambda delay: _sleep(github.read_timeout(delay))), github.pr_lock(config.repo, pr.number):
        attempt = mergify.queue_attempt(config.repo, pr.number, pr.head_sha, queue)
        state = mergify.membership(config.repo, pr.number, queue, admit_by)
        if state is None and not attempt.requested and admit_by is not None and time.monotonic() >= admit_by:
            print(f"timed out before requesting Mergify for PR #{pr.number}")
            return None
        if sha := github.merged_sha(
            config.repo, pr.number, expected_head=pr.head_sha, expected_base=pr.base_ref,
            check_hold=not attempt.requested and state is None,
        ):
            return sha
        confirmed = state is not None
        if confirmed and not attempt.confirmed:
            _require_receipt(wt, config, pr.head_sha)
            mergify.mark_confirmed(config.repo, pr.number, pr.head_sha, queue)
        if not confirmed and (attempt.confirmed or attempt.departure):
            if sha := _mergify_departure(config, pr, queue, closed := [], attempt.confirmed, attempt.departure):
                return sha
            raise closed[0]
        if not confirmed and not attempt.requested:
            _require_receipt(wt, config, pr.head_sha)
            mergify.request(config.repo, pr.number, pr.head_sha, queue)
    if deadline is not None:
        return _queue_wait(deadline, lambda remaining: _wait_for_mergify(config, pr, remaining, confirmed),
                           bounded=admit_by is not None)
    print(f"PR #{pr.number} {'confirmed in' if confirmed else 'requested from'} Mergify queue {queue}; {_FOLLOW_QUEUE}")
    return None


def cmd_merge(args: argparse.Namespace) -> int:  # noqa: C901 - cancel, wait and per-strategy merge paths
    wt, config = _context(args)
    branch = worktree.branch(wt)
    if args.cancel:
        if config.merge_strategy != "mergify":
            raise CliError(
                "merge --cancel is only available with delivery.merge = 'mergify'. "
                "For a GitHub queue entry, use Remove from queue on the pull request."
            )
        pr = github.pr_for_branch(config.repo, branch)
        if pr is None:
            raise CliError(f"No PR for branch {branch!r}. "
                           "Run `loopzero merge --cancel` from the queued PR's worktree.")
        if pr.state != "OPEN":
            raise CliError(f"PR #{pr.number} is {pr.state}: {pr.url}. There is no queue entry to cancel.")
        try:
            mergify.dequeue(config.repo, pr.number)
        except mergify.MergifyError as exc:
            raise CliError(f"{exc}. Check PR #{pr.number} in the Mergify dashboard. "
                           "If it is still queued, check the Mergify credential and rerun "
                           "`loopzero merge --cancel`, or remove it in the dashboard.") from exc
        print(f"requested removal of PR #{pr.number} from Mergify")
        return 0
    started = time.monotonic()
    deadline = None if args.wait is None else started + args.wait
    admit_by = deadline if args.wait else None
    try:
        pr, readiness = _pr_readiness(wt, config, branch, allow_merged=True, workflow_wait=args.wait is not None,
                                      mergify_deadline=admit_by)
    except github.ReadDeadline:
        print("timed out while checking merge readiness")
        return 3
    if readiness is not None and args.wait is not None and _pending_checks(readiness):
        waited = _wait_with_deadline(wt, config, pr, args.wait, kicked=False, started=started,
                                     args=args, allow_merged=True, initial=readiness,
                                     initial_read_time=time.monotonic() - started)
        if waited is None:
            return 3
        pr, readiness, _ = waited
    if pr.state == "CLOSED":
        print(f"PR #{pr.number} closed without merging", file=sys.stderr)
        _cleanup(wt, pr, closed=True)
        return 1
    if readiness is None:
        sha = github.merged_sha(config.repo, pr.number, expected_head=pr.head_sha, expected_base=config.base_branch)
        if sha is None:
            raise CliError(f"PR #{pr.number} reports MERGED but has no merge commit yet. Verify it on GitHub, then rerun `loopzero merge --wait` to finish cleanup.")
        print(f"PR #{pr.number} was already merged as {sha[:12]}; cleaning up")
    elif config.merge_strategy == "mergify":
        try:
            with github.bounded_reads(admit_by, token=None, retry=True, check_on_exit=False) if admit_by else nullcontext():
                sha = _merge_with_mergify(wt, config, pr, readiness, deadline, admit_by)
        except github.ReadDeadline:
            print(f"timed out waiting for Mergify to land PR #{pr.number}")
            return 3
    else:
        if not readiness.ready:
            hint = _merge_check_hint(config, pr, readiness)
            raise CliError("not ready to merge: " + "; ".join(_readiness_messages(pr, readiness)) + hint)
        _require_receipt(wt, config, pr.head_sha)
        sha = _merge_and_follow(config, pr, deadline, admit_by)
        if sha is None:
            if deadline is not None:
                print(f"timed out waiting for the merge queue to land PR #{pr.number}")
            else:
                print(f"PR #{pr.number} is queued for merge into {config.base_branch}; {_FOLLOW_QUEUE}")
    if sha is None:
        return 3 if deadline is not None else 0
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
    except (LoopZeroError, OSError) as exc:
        print(f"warning: merged but remote branch cleanup failed for {branch}: {exc}; "
              f"delete {branch!r} in {config.repo} if it still points at the merged head", file=sys.stderr)


def _delete_tracking_ref(wt: Path, branch: str, expected_head: str) -> None:
    if not branch.startswith("lz/") or branch.count("/") != 1:
        return
    ref = os.fsdecode(f"refs/remotes/origin/{branch}".encode("utf-8", "surrogateescape"))
    try:
        _repo_root(wt)
        current = _git(wt, "for-each-ref", "--format=%(objectname) %(refname) %(symref)", ref, paths=True).rstrip(" ")
        if current == f"{expected_head} {ref}":
            _git(wt, "update-ref", "--no-deref", "-d", ref, expected_head)
    except (LoopZeroError, OSError) as exc:
        print(f"warning: merged but tracking ref cleanup failed for {ref}: {exc}", file=sys.stderr)


def _print_display(text: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "backslashreplace").decode(encoding))


def cmd_resolve(args: argparse.Namespace) -> int:
    wt, config = _context(args)
    branch = worktree.branch(wt)
    pr = _require_pr(config, branch)
    if pr.base_ref != config.base_branch:  # trusted publishers belong to the configured base
        raise CliError(f"PR #{pr.number} targets {pr.base_ref}, configured base is {config.base_branch}. Retarget the PR on GitHub and retry.")
    publishers = config.review_publishers or (github.login(),)
    if not args.finding:
        for marker_id, _thread, f in github.open_findings(config.repo, pr.number, publishers):
            _print_display(f"{marker_id}  {f.severity}  {f.path or '-'}:{f.line or '-'}  {f.title}")
        return 0
    finding = github.resolve_finding(config.repo, pr.number, args.finding, args.reply, publishers)
    _print_display(f"resolved {args.finding}: {finding.title}")

    def has_blockers() -> bool:
        repo, number, head = config.repo, pr.number, pr.head_sha
        if not github.open_blocking_findings(repo, number, None, publishers):
            return False
        exact = eligibility.eligible_review(_pr_reviews(repo, number), head, publishers, repo=repo, number=number)
        return exact is None or bool(github.open_blocking_findings(repo, number, head, publishers))

    try:
        with github.api_error_diagnostics():
            if has_blockers():
                return 0
    except (github.GhError, _proc.ProcTimeout, OSError) as exc:
        retry = shlex.join(("gh", *github.eligibility_dispatch_args(config.repo, pr.number, config.base_branch)))
        raise CliError(
            f"Resolved finding {args.finding} on PR #{pr.number}, but could not refresh Loop-zero Eligibility. {exc}\n"
            "To check remaining findings, run `loopzero resolve`. "
            f"Then, if no findings remain, run `{retry}`."
        ) from exc
    if github.refresh_eligibility(config.repo, pr, branch, config.base_branch, has_blockers,
                                  required=eligibility.CONTEXT in config.required_ci):
        print(f"Requested a Loop-zero Eligibility refresh for PR #{pr.number}. Run `loopzero ready --wait` to follow the verdict.")
    else:
        print("Hosted eligibility workflow was not found or could not be accessed. Finding resolution is complete.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    wt = _toplevel()
    config = _load_config(args, wt)
    branch, head = worktree.branch(wt), worktree.head(wt)
    _print_display(f"worktree: {wt}\nbranch:   {branch}\nhead:     {head}\nbase:     {config.base_branch}")
    _print_display(f"dirty:    {'yes' if worktree.is_dirty(wt) else 'no'}")
    if problem := _publisher_problem(args, config):
        _print_display(f"config:   {problem}")
    pr = github.pr_for_branch(config.repo, branch)
    if pr is None:
        _print_display("pr:       none (commit changes, then open with `loopzero pr` before a long check)")
        return 0
    _print_display(f"pr:       #{pr.number} {pr.url} ({'draft' if pr.is_draft else 'ready'}, {pr.state})")
    reviews = [] if problem else _pr_reviews(config.repo, pr.number, dates=(dates := []))
    markers = [] if problem else _pr_markers(reviews, head, config.review_publishers, repo=config.repo, number=pr.number)
    if markers:
        latest = markers[-1]
        _print_display(f"review:   {latest.kind} on {latest.head[:12]} ({latest.state.lower() or 'posted'})")
    else:
        _print_display(f"review:   {'not evaluated' if problem else 'none on this PR'}")
    readiness = (github.Readiness(False, ()) if problem else
                 _readiness(wt, config, pr, head, reviews, diagnostics=True, dates=tuple(dates)))
    _print_display(f"ready:    {'yes' if readiness.ready else 'no'}")
    for reason in readiness.reasons:
        _print_display(f"  - {reason}")
    if config.merge_strategy != "mergify" or pr.state != "OPEN" or pr.head_sha != head:
        return 0
    queue = config.mergify_queue or ""
    attempt = mergify.queue_attempt(config.repo, pr.number, pr.head_sha, queue)
    state = mergify.membership(config.repo, pr.number, queue)
    label = "not admitted"
    if state:
        label = "confirmed"
    elif attempt.confirmed:
        label = "ejected"
    elif attempt.requested or (readiness.ready and attempt.admission):
        label = "waiting for admission"
    _print_display(f"mergify:  {label} ({queue})")
    if label == "waiting for admission" and attempt.admission:
        _print_display(f"  Mergify report: {attempt.admission}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return _run(parse_args(build_parser(), argv))


def _run(args: argparse.Namespace) -> int:
    try:
        with _proc.unwind_on_sigterm():
            code = globals()[f"cmd_{args.command}"](args)
        if args.command == "merge" and code == 3:
            print(_wait_resume(args))
        return code
    except IndependentReviewerUnavailable as exc:
        print(f"loopzero {args.command}: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        if isinstance(exc, sandbox.SandboxUnavailable) and args.command in {"check", "review"}:
            print(f"sandbox unavailable: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "wait", None) is not None and isinstance(
            exc, (github.GhUnavailable, _proc.ProcTimeout, mergify.MergifyUnavailable),
        ):
            print(f"loopzero {args.command}: {_one_line(exc)}. {_wait_resume(args)}", file=sys.stderr)
            return 2
        prefix = ""
        if not isinstance(exc, HANDLED):
            where = traceback.extract_tb(exc.__traceback__)[-1]
            prefix = f"unexpected error ({type(exc).__name__} at {Path(where.filename).name}:{where.lineno}): "
        print(f"loopzero {args.command}: {prefix}{_one_line(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"loopzero {args.command}: interrupted. Run `loopzero status` before resuming delivery.", file=sys.stderr)
        return 130


def _one_line(exc: BaseException) -> str:
    return " | ".join(ln.strip() for ln in (str(exc) or type(exc).__name__).splitlines() if ln.strip())


if __name__ == "__main__":
    sys.exit(_run(_module_args))
