#!/usr/bin/env python3
"""Report host-local worktree occupancy and issue-run ownership.

GitHub remains the durable remote coordination authority (`make lanes`). The
shared logical-run registry supplies issue identity; registered worktrees
supply host-local liveness. The collector never infers an issue from a branch
name and never authorizes cleanup.
"""

from __future__ import annotations

from .settings import settings

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Mapping

GIT_TIMEOUT_SECONDS = 10
MAX_STATUS_WORKERS = 8
RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")


class CollectorError(RuntimeError):
    """A required Git inventory source could not be read safely."""


class ClaimState(StrEnum):
    CURRENT = "CURRENT"
    OWNED_LIVE = "OWNED-LIVE"
    PARKED_DIRTY = "PARKED-DIRTY"
    IDLE_CLEAN = "IDLE-CLEAN"
    ATTENTION = "ATTENTION"
    UNKNOWN = "UNKNOWN"


class IssueClaimState(StrEnum):
    CURRENT = "CURRENT"
    LIVE = "LIVE"
    STALE = "STALE"


@dataclass(frozen=True)
class RegisteredWorktree:
    path: Path
    branch: str


@dataclass(frozen=True)
class ProcessProbe:
    available: bool
    cwd_by_pid: Mapping[int, Path]


@dataclass(frozen=True)
class WorktreeClaim:
    path: Path
    branch: str
    state: ClaimState
    dirty_count: int | None
    process_count: int | None
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class IssueClaim:
    run_id: str
    issue: int
    skill: str
    branch: str
    started_at: datetime | None
    worktree: Path | None
    state: IssueClaimState


def _run_git(args: list[str], *, collector: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(
            f"{collector} timed out after {GIT_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise CollectorError(f"{collector} could not start") from exc
    if result.returncode != 0:
        raise CollectorError(f"{collector} failed with exit {result.returncode}")
    return result


def current_worktree() -> Path:
    result = _run_git(
        ["git", "rev-parse", "--show-toplevel"],
        collector="current-worktree",
    )
    path = result.stdout.strip()
    if not path:
        raise CollectorError("current-worktree returned an empty path")
    return Path(path).resolve()


def parse_registered_worktrees(output: str) -> tuple[RegisteredWorktree, ...]:
    entries: list[RegisteredWorktree] = []
    path: Path | None = None
    branch = "(detached)"

    def finish() -> None:
        if path is not None:
            entries.append(RegisteredWorktree(path, branch))

    for line in output.splitlines():
        if line.startswith("worktree "):
            finish()
            path = Path(line.removeprefix("worktree "))
            branch = "(detached)"
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
    finish()
    return tuple(entries)


def registered_worktrees(root: Path) -> tuple[RegisteredWorktree, ...]:
    result = _run_git(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        collector="registered-worktrees",
    )
    return tuple(
        RegisteredWorktree(entry.path.resolve(), entry.branch)
        for entry in parse_registered_worktrees(result.stdout)
    )


def scan_process_cwds(proc_root: Path = Path("/proc")) -> ProcessProbe:
    """Read Linux process CWDs without inspecting commands or environments."""
    if not proc_root.is_dir():
        return ProcessProbe(False, {})

    cwd_by_pid: dict[int, Path] = {}
    try:
        processes = proc_root.iterdir()
    except OSError:
        return ProcessProbe(False, {})
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            cwd_by_pid[int(process.name)] = (process / "cwd").resolve(strict=True)
        except (OSError, RuntimeError):
            continue
    return ProcessProbe(True, cwd_by_pid)


def assign_processes_to_worktrees(
    worktrees: tuple[RegisteredWorktree, ...],
    cwd_by_pid: Mapping[int, Path],
) -> dict[Path, tuple[int, ...]]:
    assignments: dict[Path, list[int]] = {worktree.path: [] for worktree in worktrees}
    normalized = {
        worktree.path: worktree.path.resolve(strict=False) for worktree in worktrees
    }
    for pid, raw_cwd in cwd_by_pid.items():
        cwd = raw_cwd.resolve(strict=False)
        candidates = [
            original
            for original, path in normalized.items()
            if cwd == path or cwd.is_relative_to(path)
        ]
        if not candidates:
            continue
        owner = max(candidates, key=lambda path: len(normalized[path].parts))
        assignments[owner].append(pid)
    return {
        path: tuple(sorted(process_ids))
        for path, process_ids in assignments.items()
        if process_ids
    }


def dirty_paths(path: Path) -> tuple[str, ...] | None:
    try:
        result = _run_git(
            [
                "git",
                "-C",
                str(path),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            ],
            collector=f"worktree-status [{path}]",
        )
    except CollectorError:
        return None
    return tuple(
        record[3:]
        for record in result.stdout.split("\0")
        if len(record) >= 4
    )


def collect_dirty_paths(
    worktrees: tuple[RegisteredWorktree, ...],
) -> dict[Path, tuple[str, ...] | None]:
    """Inspect worktree status concurrently so preflight has a bounded sweep."""
    existing = [worktree.path for worktree in worktrees if worktree.path.is_dir()]
    if not existing:
        return {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(MAX_STATUS_WORKERS, len(existing))
    ) as executor:
        results = executor.map(dirty_paths, existing)
        return dict(zip(existing, results, strict=True))


def classify_worktree(
    worktree: RegisteredWorktree,
    *,
    current_worktree: Path,
    path_exists: bool,
    dirty_paths: tuple[str, ...] | None,
    process_ids: tuple[int, ...],
    process_probe_available: bool,
) -> WorktreeClaim:
    path = worktree.path
    if path.resolve(strict=False) == current_worktree.resolve(strict=False):
        provenance = ["git-worktree"]
        if dirty_paths is not None:
            provenance.append("git-status")
        if process_probe_available:
            provenance.append("process-cwd")
        return WorktreeClaim(
            path,
            worktree.branch,
            ClaimState.CURRENT,
            len(dirty_paths) if dirty_paths is not None else None,
            len(process_ids) if process_probe_available else None,
            tuple(provenance),
        )
    if not path_exists:
        provenance = ["git-worktree", "path:missing"]
        if process_probe_available:
            provenance.append("process-cwd")
        return WorktreeClaim(
            path,
            worktree.branch,
            ClaimState.ATTENTION,
            None,
            len(process_ids) if process_probe_available else None,
            tuple(provenance),
        )
    if dirty_paths is None:
        provenance = ["git-worktree", "git-status:unavailable"]
        if process_probe_available:
            provenance.append("process-cwd")
        return WorktreeClaim(
            path,
            worktree.branch,
            ClaimState.UNKNOWN,
            None,
            len(process_ids) if process_probe_available else None,
            tuple(provenance),
        )
    if not process_probe_available:
        return WorktreeClaim(
            path,
            worktree.branch,
            ClaimState.UNKNOWN,
            len(dirty_paths),
            None,
            ("git-worktree", "git-status", "process-cwd:unavailable"),
        )
    if process_ids:
        return WorktreeClaim(
            path,
            worktree.branch,
            ClaimState.OWNED_LIVE,
            len(dirty_paths),
            len(process_ids),
            ("git-worktree", "git-status", "process-cwd"),
        )
    if dirty_paths:
        return WorktreeClaim(
            path,
            worktree.branch,
            ClaimState.PARKED_DIRTY,
            len(dirty_paths),
            0,
            ("git-worktree", "git-status", "process-cwd"),
        )
    return WorktreeClaim(
        path,
        worktree.branch,
        ClaimState.IDLE_CLEAN,
        0,
        0,
        ("git-worktree", "git-status", "process-cwd"),
    )


def collect_claims(root: Path | None = None) -> tuple[WorktreeClaim, ...]:
    current = (root or current_worktree()).resolve()
    worktrees = registered_worktrees(current)
    probe = scan_process_cwds()
    assignments = assign_processes_to_worktrees(worktrees, probe.cwd_by_pid)
    statuses = collect_dirty_paths(worktrees)
    return tuple(
        classify_worktree(
            worktree,
            current_worktree=current,
            path_exists=worktree.path.is_dir(),
            dirty_paths=statuses.get(worktree.path),
            process_ids=assignments.get(worktree.path, ()),
            process_probe_available=probe.available,
        )
        for worktree in worktrees
    )


def shared_repo_root(root: Path) -> Path:
    result = _run_git(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        collector="git-common-dir",
    )
    path = result.stdout.strip()
    if not path:
        raise CollectorError("git-common-dir returned an empty path")
    return Path(path).resolve().parent


def load_skill_runs(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    directory = root / settings.audit_root / "skill-runs"
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CollectorError(f"skill-run registry is unreadable [{path}]") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def collect_issue_claims(
    entries: list[dict[str, object]],
    worktree_claims: tuple[WorktreeClaim, ...],
) -> tuple[IssueClaim, ...]:
    """Join active issue runs to registered worktrees.

    The append-only run registry decides whether a claim is active. The
    worktree inventory decides whether that active owner is live, current, or
    stale because its worktree disappeared. Process counts are deliberately
    not lease authority: an executor may be between foreground turns or held
    by a detached continuation.
    """
    latest_by_run: dict[str, tuple[datetime, int, dict[str, object]]] = {}
    started_by_run: dict[str, datetime] = {}
    for index, entry in enumerate(entries):
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            continue
        timestamp = _parse_timestamp(entry.get("ts"))
        rank = (timestamp or datetime.min.replace(tzinfo=UTC), index)
        previous = latest_by_run.get(run_id)
        if previous is None or rank[:2] > previous[:2]:
            latest_by_run[run_id] = (*rank, entry)
        if entry.get("outcome") == "in_progress":
            if timestamp is not None:
                started_by_run[run_id] = min(
                    timestamp,
                    started_by_run.get(run_id, timestamp),
                )

    # Git prevents one attached branch from being checked out twice. Sentinel
    # detached/unknown names are not identities and therefore cannot own an
    # issue claim by branch join.
    by_branch = {
        claim.branch: claim
        for claim in worktree_claims
        if claim.branch not in {"(detached)", "(unknown)"}
    }
    claims: list[IssueClaim] = []
    for run_id, (_timestamp, _index, entry) in latest_by_run.items():
        issue = entry.get("issue")
        branch = entry.get("git_branch")
        if (
            entry.get("outcome") != "in_progress"
            or not isinstance(issue, int)
            or isinstance(issue, bool)
            or not isinstance(branch, str)
            or not branch
        ):
            continue
        worktree_claim = by_branch.get(branch)
        if worktree_claim is None or worktree_claim.state == ClaimState.ATTENTION:
            worktree = worktree_claim.path if worktree_claim is not None else None
            state = IssueClaimState.STALE
        elif worktree_claim.state == ClaimState.CURRENT:
            worktree = worktree_claim.path
            state = IssueClaimState.CURRENT
        else:
            worktree = worktree_claim.path
            state = IssueClaimState.LIVE
        skill = entry.get("skill")
        claims.append(
            IssueClaim(
                run_id=run_id,
                issue=issue,
                skill=skill if isinstance(skill, str) and skill else "(unknown)",
                branch=branch,
                started_at=started_by_run.get(run_id),
                worktree=worktree,
                state=state,
            )
        )
    return tuple(sorted(claims, key=lambda claim: (claim.issue, claim.run_id)))


def _display_path(path: Path, display_root: Path) -> str:
    if path.resolve(strict=False) == display_root.resolve(strict=False):
        return "."
    home = Path.home()
    if path.is_relative_to(home):
        return "~/" + str(path.relative_to(home))
    return str(path)


def _format_age(started_at: datetime | None, now: datetime) -> str:
    if started_at is None:
        return "unknown"
    age = max(timedelta(0), now.astimezone(UTC) - started_at)
    seconds = int(age.total_seconds())
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _release_command(claim: IssueClaim) -> str:
    fields = (
        "python3 scripts/util/skill_run_log.py"
        f" --skill {shlex.quote(claim.skill)}"
        f" --run-id {shlex.quote(claim.run_id)}"
        f" --issue {claim.issue}"
        f" --git-branch {shlex.quote(claim.branch)}"
        " --outcome abandoned"
    )
    return fields


def render_issue_claims(
    claims: tuple[IssueClaim, ...],
    *,
    display_root: Path,
    now: datetime | None = None,
) -> str:
    if not claims:
        return "Live issue claims (shared run registry): none"
    observed_at = now or datetime.now(UTC)
    lines = [
        "Live issue claims (shared run registry)",
        "ISSUE | RUN | STATE | AGE | WORKTREE | BRANCH",
    ]
    for claim in claims:
        worktree = (
            _display_path(claim.worktree, display_root)
            if claim.worktree is not None
            else "(missing)"
        )
        lines.append(
            f"#{claim.issue} | {claim.run_id} | {claim.state.value} | "
            f"{_format_age(claim.started_at, observed_at)} | {worktree} | {claim.branch}"
        )
    lines.append(
        "LIVE claims in another worktree block issue intake; CURRENT is the caller's "
        "own run; STALE claims do not block and must be released explicitly."
    )
    return "\n".join(lines)


def render_issue_guard(
    claims: tuple[IssueClaim, ...],
    *,
    issue: int,
    now: datetime | None = None,
) -> tuple[str, int]:
    observed_at = now or datetime.now(UTC)
    relevant = tuple(claim for claim in claims if claim.issue == issue)
    live = tuple(claim for claim in relevant if claim.state == IssueClaimState.LIVE)
    stale = tuple(claim for claim in relevant if claim.state == IssueClaimState.STALE)
    lines: list[str] = []
    for claim in live:
        worktree = str(claim.worktree) if claim.worktree is not None else "(missing)"
        lines.append(
            f"LIVE ISSUE CLAIM: #{issue} is held by {claim.run_id} in {worktree} "
            f"(branch {claim.branch}, age {_format_age(claim.started_at, observed_at)})."
        )
    if live:
        lines.append("  Re-enter or coordinate with the owning run; do NOT duplicate intake.")
    for claim in stale:
        worktree = str(claim.worktree) if claim.worktree is not None else "(missing)"
        lines.append(
            f"STALE CLAIM: #{issue} run {claim.run_id} has no live worktree at "
            f"{worktree} (branch {claim.branch}, age "
            f"{_format_age(claim.started_at, observed_at)}); it does not block."
        )
        lines.append(f"  Release with: {_release_command(claim)}")
    return "\n".join(lines), 3 if live else 0


def render_claims(
    claims: tuple[WorktreeClaim, ...],
    *,
    display_root: Path,
    include_heading: bool = True,
) -> str:
    lines: list[str] = []
    if include_heading:
        lines.append("🏠 Local worktree ownership gate (read-only)")
    if not claims:
        lines.append("  no registered worktrees")
        return "\n".join(lines)
    lines.append("STATE | branch | dirty | processes | worktree | provenance")
    for claim in claims:
        dirty = "?" if claim.dirty_count is None else str(claim.dirty_count)
        processes = "?" if claim.process_count is None else str(claim.process_count)
        lines.append(
            f"{claim.state.value} | {claim.branch} | {dirty} | {processes} | "
            f"{_display_path(claim.path, display_root)} | {','.join(claim.provenance)}"
        )
    lines.append(
        "Local occupancy is one gate; `make lanes` remains the independent remote gate."
    )
    lines.append(
        "For a matching non-current lane: stop on OWNED-LIVE, PARKED-DIRTY, "
        "ATTENTION, or UNKNOWN; re-enter or remove IDLE-CLEAN before reuse."
    )
    return "\n".join(lines)


def _json_claim(claim: WorktreeClaim) -> dict[str, object]:
    value = asdict(claim)
    value["path"] = str(claim.path)
    value["state"] = claim.state.value
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show host-local worktree occupancy before lane selection."
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-heading", action="store_true")
    parser.add_argument("--issue", type=int)
    parser.add_argument(
        "--guard",
        action="store_true",
        help="Return 3 when --issue is claimed by another live worktree",
    )
    args = parser.parse_args()
    if args.guard and args.issue is None:
        parser.error("--guard requires --issue")
    try:
        root = current_worktree()
        claims = collect_claims(root)
        issue_claims = collect_issue_claims(
            load_skill_runs(shared_repo_root(root)),
            claims,
        )
    except (CollectorError, OSError, subprocess.SubprocessError) as exc:
        print(f"🏠 Local worktree ownership gate unavailable: {exc}", file=sys.stderr)
        return 1
    if args.guard:
        rendered, exit_code = render_issue_guard(issue_claims, issue=args.issue)
        if rendered:
            print(rendered, file=sys.stderr)
        return exit_code
    if args.json:
        print(json.dumps([_json_claim(claim) for claim in claims], indent=2))
    else:
        print(
            render_claims(
                claims,
                display_root=root,
                include_heading=not args.no_heading,
            )
        )
        print()
        print(render_issue_claims(issue_claims, display_root=root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
