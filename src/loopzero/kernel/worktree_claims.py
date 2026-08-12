#!/usr/bin/env python3
"""Report host-local ownership signals for every registered Git worktree.

This is the local half of lane-selection safety. GitHub remains the durable
remote coordination authority (`make lanes`). This collector answers only
whether an exact local worktree is current, occupied by a live process, parked
with changes, idle and clean, or unverifiable. It never infers an issue or task
from a branch name and never authorizes cleanup.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

GIT_TIMEOUT_SECONDS = 10
MAX_STATUS_WORKERS = 8


class CollectorError(RuntimeError):
    """A required Git inventory source could not be read safely."""


class ClaimState(StrEnum):
    CURRENT = "CURRENT"
    OWNED_LIVE = "OWNED-LIVE"
    PARKED_DIRTY = "PARKED-DIRTY"
    IDLE_CLEAN = "IDLE-CLEAN"
    ATTENTION = "ATTENTION"
    UNKNOWN = "UNKNOWN"


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


def _display_path(path: Path, display_root: Path) -> str:
    if path.resolve(strict=False) == display_root.resolve(strict=False):
        return "."
    home = Path.home()
    if path.is_relative_to(home):
        return "~/" + str(path.relative_to(home))
    return str(path)


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
    args = parser.parse_args()
    try:
        root = current_worktree()
        claims = collect_claims(root)
    except (CollectorError, OSError, subprocess.SubprocessError) as exc:
        print(f"🏠 Local worktree ownership gate unavailable: {exc}", file=sys.stderr)
        return 1
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
