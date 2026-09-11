#!/usr/bin/env python3
"""Read-only liveness signals for local delivery work.

The signal source is deliberately local and cheap: shared JSONL telemetry under
``.audit`` and the worktrees registered with Git. This module does not import
product code and never changes the repository, telemetry, or worktrees.
"""

from __future__ import annotations

from .settings import settings

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

ACTIVE_RUN_EVENT_STALE_AFTER = timedelta(hours=2)
DIRTY_WORKTREE_STALE_AFTER = timedelta(hours=2)
ACTIVE_LOGICAL_RUN_MAX_AGE = timedelta(hours=24)
GIT_COMMAND_TIMEOUT_SECONDS = 10

TERMINAL_OUTCOMES = {
    "merged",
    "abandoned",
    "blocked",
    "resolved_no_change",
}
ROLLOVER_RECOMMENDATION = "re-entry capsule rollover"
RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")


class CollectorError(RuntimeError):
    """A liveness collector failed before it could report trustworthy state."""


def _run_git(
    args: list[str], *, collector: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one Git collector with a hard deadline and an actionable error."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(
            f"{collector} timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise CollectorError(
            f"{collector} could not start ({type(exc).__name__})"
        ) from exc
    if result.returncode != 0:
        raise CollectorError(f"{collector} failed with exit {result.returncode}")
    return result


@dataclass(frozen=True)
class RegisteredWorktree:
    path: Path
    branch: str


@dataclass(frozen=True)
class ActiveRun:
    run_id: str
    skill: str
    branch: str
    started_at: datetime


@dataclass(frozen=True)
class StalledRun:
    run: ActiveRun
    last_event_at: datetime | None
    idle_for: timedelta


@dataclass(frozen=True)
class AgedDirtyWorktree:
    worktree: RegisteredWorktree
    dirty_paths: tuple[str, ...]
    latest_activity_at: datetime
    idle_for: timedelta


@dataclass(frozen=True)
class LongRunning:
    run: ActiveRun
    age: timedelta
    recommendation: str = ROLLOVER_RECOMMENDATION


@dataclass(frozen=True)
class LivenessReport:
    stalled_runs: tuple[StalledRun, ...]
    aged_dirty_worktrees: tuple[AgedDirtyWorktree, ...]
    long_running: tuple[LongRunning, ...]

    @property
    def finding_count(self) -> int:
        return (
            len(self.stalled_runs)
            + len(self.aged_dirty_worktrees)
            + len(self.long_running)
        )


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def read_jsonl(directory: Path) -> list[dict[str, object]]:
    """Read valid object rows from all JSONL files in deterministic order."""
    rows: list[dict[str, object]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def repo_root() -> Path:
    """Return the shared checkout root used by local audit telemetry."""
    result = _run_git(
        ["git", "rev-parse", "--git-common-dir"],
        collector="git-common-dir",
    )
    if not result.stdout.strip():
        raise CollectorError("git-common-dir returned an empty path")
    return Path(result.stdout.strip()).resolve().parent


def parse_registered_worktrees(output: str) -> tuple[RegisteredWorktree, ...]:
    """Parse ``git worktree list --porcelain`` output."""
    entries: list[RegisteredWorktree] = []
    path: Path | None = None
    branch = "(detached)"

    def finish() -> None:
        if path is not None:
            entries.append(RegisteredWorktree(path.resolve(), branch))

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
    """Read all worktrees registered for the repository at ``root``."""
    result = _run_git(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        collector=f"registered-worktrees [{root}]",
    )
    return parse_registered_worktrees(result.stdout)


def _worktree_index_path(worktree: Path) -> Path | None:
    """Resolve the index path for both primary and linked worktrees."""
    git_path = worktree / ".git"
    try:
        if git_path.is_dir():
            return git_path / "index"
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                raw_gitdir = pointer.removeprefix("gitdir:").strip()
                linked_gitdir = Path(raw_gitdir)
                if not linked_gitdir.is_absolute():
                    linked_gitdir = git_path.parent / linked_gitdir
                return linked_gitdir.resolve() / "index"
    except OSError:
        return None
    return None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _status_paths(worktree: Path) -> tuple[str, ...]:
    """Return dirty paths, failing closed when Git cannot inspect the worktree."""
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = _run_git(
        [
            "git",
            "-C",
            str(worktree),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ],
        collector=f"worktree-status [{worktree}]",
        environment=environment,
    )

    paths: list[str] = []
    for record in result.stdout.split("\0"):
        if len(record) < 4:
            continue
        paths.append(record[3:])
    return tuple(paths)


def _path_activity_mtime(path: Path, worktree: Path) -> datetime | None:
    """Use the nearest existing path so fresh unstaged deletions stay fresh.

    ``lstat`` keeps a symlink's activity independent from the target it points
    to, including for broken symlinks.
    """
    try:
        path.relative_to(worktree)
    except ValueError:
        return None

    candidate = path
    while True:
        try:
            return datetime.fromtimestamp(candidate.lstat().st_mtime, tz=UTC)
        except OSError:
            pass
        if candidate == worktree or candidate == candidate.parent:
            return None
        candidate = candidate.parent


def inspect_dirty_worktree(
    worktree: RegisteredWorktree, now: datetime
) -> AgedDirtyWorktree | None:
    """Inspect one registered worktree without updating its Git index."""
    if not worktree.path.is_dir():
        return None

    index_path = _worktree_index_path(worktree.path)
    index_mtime = _mtime(index_path) if index_path is not None else None
    dirty_paths = _status_paths(worktree.path)
    if not dirty_paths:
        return None

    activity_times = [timestamp for timestamp in (index_mtime,) if timestamp]
    for relative_path in dirty_paths:
        path = Path(relative_path)
        if not path.is_absolute():
            path = worktree.path / path
        if timestamp := _path_activity_mtime(path, worktree.path):
            activity_times.append(timestamp)
    if not activity_times:
        return None

    latest_activity_at = max(activity_times)
    idle_for = max(timedelta(0), now - latest_activity_at)
    if idle_for <= DIRTY_WORKTREE_STALE_AFTER:
        return None
    return AgedDirtyWorktree(
        worktree=worktree,
        dirty_paths=dirty_paths,
        latest_activity_at=latest_activity_at,
        idle_for=idle_for,
    )


def active_runs(entries: Iterable[dict[str, object]]) -> tuple[ActiveRun, ...]:
    """Collapse lifecycle rows and return runs whose latest state is active."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        outcome = entry.get("outcome")
        if outcome not in TERMINAL_OUTCOMES and outcome != "in_progress":
            continue
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            continue
        grouped.setdefault(run_id, []).append(entry)

    active: list[ActiveRun] = []
    for run_id, rows in grouped.items():
        _, latest = max(
            enumerate(rows),
            key=lambda indexed: (
                parse_timestamp(indexed[1].get("ts"))
                or datetime.min.replace(tzinfo=UTC),
                indexed[0],
            ),
        )
        if latest.get("outcome") != "in_progress":
            continue
        timestamps = [
            timestamp
            for row in rows
            if row.get("outcome") == "in_progress"
            if (timestamp := parse_timestamp(row.get("ts"))) is not None
        ]
        if not timestamps:
            continue
        branch = latest.get("git_branch")
        skill = latest.get("skill")
        if not isinstance(branch, str) or not branch:
            continue
        if not isinstance(skill, str) or not skill:
            skill = "(unknown)"
        active.append(
            ActiveRun(
                run_id=run_id,
                skill=skill,
                branch=branch,
                started_at=min(timestamps),
            )
        )
    return tuple(sorted(active, key=lambda run: (run.branch, run.run_id)))


def latest_branch_events(
    events: Iterable[dict[str, object]],
) -> dict[str, datetime]:
    """Return the newest parseable event timestamp for each Git branch."""
    latest: dict[str, datetime] = {}
    for event in events:
        branch = event.get("git_branch")
        timestamp = parse_timestamp(event.get("ts"))
        if not isinstance(branch, str) or not branch or timestamp is None:
            continue
        if timestamp > latest.get(branch, datetime.min.replace(tzinfo=UTC)):
            latest[branch] = timestamp
    return latest


def find_stalled_runs(
    entries: Iterable[dict[str, object]],
    events: Iterable[dict[str, object]],
    now: datetime,
) -> tuple[StalledRun, ...]:
    """Find active runs with more than two hours since branch activity."""
    branch_events = latest_branch_events(events)
    findings: list[StalledRun] = []
    for run in active_runs(entries):
        last_event = branch_events.get(run.branch)
        baseline = max(run.started_at, last_event) if last_event else run.started_at
        idle_for = max(timedelta(0), now - baseline)
        if idle_for > ACTIVE_RUN_EVENT_STALE_AFTER:
            findings.append(StalledRun(run, last_event, idle_for))
    return tuple(findings)


def find_aged_dirty_worktrees(
    worktrees: Iterable[RegisteredWorktree],
    now: datetime,
    inspector: Callable[[RegisteredWorktree, datetime], AgedDirtyWorktree | None]
    | None = None,
) -> tuple[AgedDirtyWorktree, ...]:
    """Find registered dirty worktrees with old filesystem activity."""
    inspect = inspector or inspect_dirty_worktree
    findings = [
        finding
        for worktree in worktrees
        if (finding := inspect(worktree, now)) is not None
    ]
    return tuple(sorted(findings, key=lambda finding: str(finding.worktree.path)))


def find_long_running(
    entries: Iterable[dict[str, object]], now: datetime
) -> tuple[LongRunning, ...]:
    """Find active logical runs older than one day."""
    findings = [
        LongRunning(run, max(timedelta(0), now - run.started_at))
        for run in active_runs(entries)
        if now - run.started_at > ACTIVE_LOGICAL_RUN_MAX_AGE
    ]
    return tuple(
        sorted(
            findings,
            key=lambda finding: (finding.run.started_at, finding.run.run_id),
        )
    )


def build_report(
    skill_runs: Iterable[dict[str, object]],
    agent_events: Iterable[dict[str, object]],
    worktrees: Iterable[RegisteredWorktree],
    now: datetime | None = None,
    inspector: Callable[[RegisteredWorktree, datetime], AgedDirtyWorktree | None]
    | None = None,
) -> LivenessReport:
    """Build all three bounded liveness signals from supplied evidence."""
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    observed_at = observed_at.astimezone(UTC)
    skill_runs = list(skill_runs)
    return LivenessReport(
        stalled_runs=find_stalled_runs(skill_runs, agent_events, observed_at),
        aged_dirty_worktrees=find_aged_dirty_worktrees(
            worktrees, observed_at, inspector
        ),
        long_running=find_long_running(skill_runs, observed_at),
    )


def collect_report(
    root: Path | None = None, now: datetime | None = None
) -> LivenessReport:
    """Read shared local evidence and return a liveness report."""
    root = root or repo_root()
    audit = root / settings.audit_root
    return build_report(
        read_jsonl(audit / "skill-runs"),
        read_jsonl(audit / "agent-events"),
        registered_worktrees(root),
        now,
    )


def _format_age(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _format_timestamp(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "never"
    return (
        timestamp.astimezone(UTC).isoformat(timespec="minutes").replace("+00:00", "Z")
    )


def render(report: LivenessReport) -> str:
    """Render one stable human-readable report for both Make entry points."""
    lines = [
        "🚦 Delivery liveness (read-only)",
        "  active runs without a branch event for >2h: " f"{len(report.stalled_runs)}",
    ]
    for finding in report.stalled_runs:
        run = finding.run
        lines.append(
            f"    - {run.skill} run={run.run_id} "
            f"branch={run.branch} idle={_format_age(finding.idle_for)} "
            f"last_event={_format_timestamp(finding.last_event_at)}"
        )
    lines.append(
        "  dirty registered worktrees with latest path/index activity for >2h: "
        f"{len(report.aged_dirty_worktrees)}"
    )
    for finding in report.aged_dirty_worktrees:
        worktree = finding.worktree
        lines.append(
            f"    - {worktree.path} branch={worktree.branch} "
            f"idle={_format_age(finding.idle_for)} "
            f"latest_activity={_format_timestamp(finding.latest_activity_at)} "
            f"dirty_paths={len(finding.dirty_paths)}"
        )
    lines.append(
        "  active logical runs for >24h (recommend re-entry capsule rollover): "
        f"{len(report.long_running)}"
    )
    for finding in report.long_running:
        run = finding.run
        lines.append(
            f"    - {run.skill} run={run.run_id} branch={run.branch} "
            f"age={_format_age(finding.age)} recommendation={finding.recommendation}"
        )
    if report.finding_count == 0:
        lines.append("  no anomalies — exception report, not active-work inventory")
    return "\n".join(lines)


def main() -> int:
    try:
        report = collect_report()
    except (CollectorError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"🚦 Delivery liveness (read-only) unavailable: {exc}", file=sys.stderr)
        return 1
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
