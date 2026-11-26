"""Shared dataclasses used across loopzero modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BLOCKING = ("critical", "important")  # finding severities that block readiness


def is_sha(value: object) -> bool:
    """A full lowercase 40-hex Git object id."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


class LoopZeroError(Exception):
    """Base class for typed loopzero failures; the message is user-facing."""


@dataclass(frozen=True)
class ResourceLimits:
    memory_mb: int = 4096
    file_mb: int = 2048


@dataclass(frozen=True)
class ReviewConfig:
    model: str | None = None
    effort: str | None = None
    allowed_efforts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    repo: str  # "owner/name"
    base_branch: str  # "main"
    checks: tuple[str, ...]  # shell commands run in sandbox, in order
    required_ci: tuple[str, ...]  # exact GitHub check names that must be green
    merge_strategy: str  # "squash" | "merge" | "rebase" | "queue" | "mergify"
    reviewers: tuple[str, ...]  # ordered preference: ("claude", "codex")
    reviewer_ro_paths: tuple[str, ...] = ()  # reviewer CLI install/runtime paths
    review_publishers: tuple[str, ...] = ()  # trusted GitHub logins, independent of CI token
    network: bool = False  # allow network inside sandbox
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
    sandbox_ro: tuple[str, ...] = ()  # extra host paths exposed read-only in the sandbox
    writable: tuple[str, ...] = ()  # host paths bound read-write (shared tool caches)
    scratch: tuple[str, ...] = (".venv", ".ruff_cache", ".pytest_cache", "node_modules/.cache")
    env: tuple[tuple[str, str], ...] = ()  # fixed variables set inside the sandbox; win over host
    limits: ResourceLimits = ResourceLimits()  # best-effort per-check resource limits
    review: dict[str, ReviewConfig] = field(default_factory=dict)
    mergify_queue: str | None = None
    inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    trusted_base_revision: str | None = field(default=None, compare=False)


def is_check_exit_code(value: object) -> bool:
    """Recognize JSON integer exit codes without accepting booleans."""
    return type(value) is int


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    duration_s: float
    tail: str  # last 40 lines of combined output, up to 16 KiB of UTF-8
    input_key: str | None = None
    reused_from: str | None = None


@dataclass(frozen=True)
class CheckReport:
    head: str
    dirty: bool
    results: tuple[CheckResult, ...]
    settings_sha256: str | None = None

    @property
    def ok(self) -> bool:
        """True when the report has results and every check exited 0."""
        return bool(self.results) and all(result.exit_code == 0 for result in self.results)


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
    raw: str  # complete native envelope(s), retained privately; public views are sanitized
    model: str | None = None
    duration_s: float | None = None
    provenance: dict[str, object] = field(default_factory=dict)
    chunk_count: int = 1
    effort: str | None = None
    fallback: str = ""
