"""Shared dataclasses used across loopzero modules."""

from __future__ import annotations

from dataclasses import dataclass


class LoopZeroError(Exception):
    """Base class for typed loopzero failures; the message is user-facing."""


@dataclass(frozen=True)
class ResourceLimits:
    memory_mb: int = 4096
    processes: int = 512
    file_mb: int = 2048


@dataclass(frozen=True)
class Config:
    repo: str  # "owner/name"
    base_branch: str  # "main"
    checks: tuple[str, ...]  # shell commands run in sandbox, in order
    required_ci: tuple[str, ...]  # exact GitHub check names that must be green
    merge_strategy: str  # "squash" | "merge" | "rebase"
    reviewers: tuple[str, ...]  # ordered preference: ("claude", "codex")
    reviewer_ro_paths: tuple[str, ...] = ()  # reviewer CLI install/runtime paths
    network: bool = False  # allow network inside sandbox
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
    sandbox_ro: tuple[str, ...] = ()  # extra host paths exposed read-only in the sandbox
    writable: tuple[str, ...] = ()  # host paths bound read-write (shared tool caches)
    scratch: tuple[str, ...] = (".venv", ".ruff_cache", ".pytest_cache", "node_modules/.cache")
    env: tuple[tuple[str, str], ...] = ()  # fixed variables set inside the sandbox; win over host
    limits: ResourceLimits = ResourceLimits()  # best-effort per-check shell resource limits


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    duration_s: float
    tail: str  # last 40 lines of combined output


@dataclass(frozen=True)
class CheckReport:
    head: str
    dirty: bool
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """True when every check exited 0."""
        return all(result.exit_code == 0 for result in self.results)


@dataclass(frozen=True)
class Finding:
    severity: str  # "critical" | "important" | "suggestion"
    path: str | None
    line: int | None
    title: str
    body: str


@dataclass(frozen=True)
class ReviewResult:
    family: str  # "claude" | "codex"
    head: str
    kind: str  # "primary" | "delta"
    verdict: str  # "approve" | "request_changes"
    findings: tuple[Finding, ...]
    raw: str  # untouched model output for the PR comment
    model: str | None = None
    duration_s: float | None = None
