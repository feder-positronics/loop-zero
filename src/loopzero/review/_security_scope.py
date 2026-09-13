"""Canonical, stateless security-review scope derivation for exact Git trees."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Protocol

# Keep direct module loading and isolated script execution sibling-import safe.

from ..kernel.gitscope import DispatchError, trusted_git_command
from ..kernel.sandbox import environment as sandbox_environment

_ALWAYS_SECURITY_REVIEW_PATTERNS = (
    "*dependencies*.py",
    "*middleware*",
    "*/integrations/*",
    "*config.py",
    "*.env*",
)
_SECURITY_CONFIGURATION_FILENAMES = {
    ".npmrc",
    ".pypirc",
    ".yarnrc",
    "bun.lock",
    "bun.lockb",
    "package-lock.json",
    "package.json",
    "pipfile",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pyproject.toml",
    "constraints.txt",
    "requirements.txt",
    "uv.lock",
    "yarn.lock",
}
_UNTRACKED_CONFIGURATION_SUFFIXES = (
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".sql",
    ".tf",
    ".tfvars",
    ".toml",
    ".yaml",
    ".yml",
)
_UNTRACKED_EXECUTABLE_SUFFIXES = (
    ".bash",
    ".html",
    ".js",
    ".jsx",
    ".ps1",
    ".py",
    ".sh",
    ".ts",
    ".tsx",
    ".zsh",
)
_SHELL_EXECUTABLE_SUFFIXES = (".bash", ".ps1", ".sh", ".zsh")
# Makefiles are classified by changed-hunk content, not by name (#4015):
# 0/513 completed trust verifications ever produced a Makefile-carried finding,
# so only dependency-install, deploy/privilege, and trusted-execution recipe
# changes trigger; argument/telemetry plumbing passes. Unparseable, binary,
# renamed, or type-changed makefile rows fail closed.
_MAKEFILE_TRUST_CHANGE = re.compile(
    r"\b(?:uv|pip3?|pipx|pnpm|npm|yarn|apt(?:-get)?|brew)\b[^\n]*"
    r"\b(?:install|add|sync|update|upgrade)\b"
    r"|\b(?:curl|wget)\b"
    r"|\b(?:railway|vercel|deploy|publish|release|docker|sudo|chmod|chown|ssh|scp)\b"
    r"|^\s*(?:override\s+)?(?:SHELL|\.SHELLFLAGS|PATH)\s*[:+?!]?="
    r"|^\s*(?:un)?export\s"
    r"|agent_dispatch|pr_closeout|pr_publish|pr_merge_gate|final_ci_gate"
    r"|guardian|job\.sh|commit-auto-fix|pre-commit|\bhooks?\b"
    r"|python3?\s+-I\b|/usr/bin/",
    re.IGNORECASE | re.MULTILINE,
)
_SECURITY_CHANGE = re.compile(
    r"auth|credential|secret|session|token|api[_-]?key|https?://|"
    r"@(?:\w+\.)?(?:get|post|put|patch|delete)\b|Depends\(|"
    r"CurrentUser\b|CurrentAdmin\b|current_active_user\b",
    re.IGNORECASE,
)
_OWNERSHIP_CHANGE = re.compile(
    r"(?:account|actor|owner|tenant|user)_ids?\b|actor_kind\b|"
    r"uploader_id\b|current_user\b|created_by\b|ownership(?:_scope)?\b|"
    r"access_scope\b|dedup_scope\b",
    re.IGNORECASE,
)
_SERVER_ACTION_CHANGE = re.compile(
    r"export\s+async\s+function|auth|session|token|https?://", re.IGNORECASE
)
_DEFAULT_SECURITY_PATTERNS = _ALWAYS_SECURITY_REVIEW_PATTERNS
_REQUIRED_SECTIONS: tuple[str, ...] = ("code",)


def configure(
    *, security_patterns: tuple[str, ...], required_sections: tuple[str, ...]
) -> None:
    """Install consumer-owned path patterns and terminal section names."""
    global _ALWAYS_SECURITY_REVIEW_PATTERNS, _REQUIRED_SECTIONS
    _ALWAYS_SECURITY_REVIEW_PATTERNS = (
        tuple(security_patterns) or _DEFAULT_SECURITY_PATTERNS
    )
    _REQUIRED_SECTIONS = tuple(
        section for section in dict.fromkeys(required_sections)
        if section != "security"
    ) or ("code",)


class SecurityReviewScopeError(RuntimeError):
    """The exact Git range cannot be classified without ambiguity."""


class CommandOutcome(Protocol):
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> CommandOutcome: ...


def _security_configuration_path(path: str) -> bool:
    lower = path.lower()
    name = lower.rsplit("/", maxsplit=1)[-1]
    return bool(
        name in _SECURITY_CONFIGURATION_FILENAMES
        or name.startswith("dockerfile")
        or (
            name.startswith(("requirements", "constraints"))
            and name.endswith((".in", ".txt"))
        )
        or lower.endswith(_UNTRACKED_CONFIGURATION_SUFFIXES)
    )


def _makefile_path(path: str) -> bool:
    return path.lower().rsplit("/", maxsplit=1)[-1] == "makefile"


def required_review_sections(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return the closed terminal-review section set for canonical paths."""
    sections = tuple(dict.fromkeys(_REQUIRED_SECTIONS))
    if paths and "security" not in sections:
        sections += ("security",)
    return sections


def untracked_security_trigger_paths(paths: list[str]) -> tuple[str, ...]:
    """Conservatively classify untracked paths without reading local content."""
    triggered: set[str] = set()
    for path in paths:
        lower = path.lower()
        configuration_path = _security_configuration_path(path) or _makefile_path(
            path
        )
        if any(
            marker in f"/{lower}"
            for marker in ("/tests/", "/__tests__/", ".test.", ".spec.")
        ) and not configuration_path:
            continue
        if path.startswith("docs/") and path.endswith((".md", ".mdx", ".html")):
            continue
        if (
            configuration_path
            or lower.endswith(_UNTRACKED_EXECUTABLE_SUFFIXES)
        ):
            triggered.add(path)
    return tuple(sorted(triggered))


def security_trigger_paths_between(
    worktree: Path,
    base_ref: str,
    head_ref: str,
    *,
    runner: CommandRunner | None = None,
) -> tuple[str, ...]:
    """Evaluate the canonical security triggers between two exact trees."""
    if runner is None:
        class GitRunner:
            def run(self, args: list[str], *, check: bool = True):
                command = (
                    trusted_git_command(worktree, *args[1:])
                    if args and args[0] == "git"
                    else args
                )
                completed = subprocess.run(
                    command,
                    cwd=worktree,
                    env=sandbox_environment(os.environ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if check and completed.returncode:
                    raise DispatchError(completed.stderr.strip() or "git failed")
                return completed
        git = GitRunner()
    else:
        git = runner

    def run(args: list[str], *, check: bool = True) -> CommandOutcome:
        try:
            return git.run(args, check=check)
        except (DispatchError, OSError, RuntimeError) as exc:
            raise SecurityReviewScopeError(str(exc)) from exc

    raw_names = run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--no-ext-diff",
            base_ref,
            head_ref,
            "--",
        ]
    ).stdout
    fields = raw_names.split("\0")
    rows: list[tuple[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise SecurityReviewScopeError(
                "malformed NUL-delimited git name-status output"
            )
        paths = fields[index : index + path_count]
        if len(paths) != path_count or any(not path for path in paths):
            raise SecurityReviewScopeError(
                "malformed NUL-delimited git name-status output"
            )
        rows.extend((status, path) for path in paths)
        index += path_count

    triggered: set[str] = set()
    for status, path in rows:
        lower = path.lower()
        dependency_manifest = lower.rsplit("/", maxsplit=1)[-1] in {
            "package.json",
            "pyproject.toml",
        }
        makefile_path = _makefile_path(path)
        if _security_configuration_path(path) and not dependency_manifest:
            triggered.add(path)
            continue
        if lower.endswith(_SHELL_EXECUTABLE_SUFFIXES):
            triggered.add(path)
            continue
        if not makefile_path and any(
            marker in f"/{lower}"
            for marker in ("/tests/", "/__tests__/", ".test.", ".spec.")
        ):
            continue
        if path.startswith("docs/") and path.endswith((".md", ".mdx", ".html")):
            continue
        patch = run(
            [
                "git",
                "diff",
                "--unified=0",
                "--no-ext-diff",
                base_ref,
                head_ref,
                "--",
                path,
            ]
        ).stdout
        changed_lines = "\n".join(
            line[1:]
            for line in patch.splitlines()
            if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
        )
        if makefile_path:
            # Zero-context hunks omit the enclosing recipe, so a benign-looking
            # command appended inside a trust-bearing target would otherwise
            # escape. Git's hunk headers carry the nearest column-0 line (the
            # owning target for tab-indented recipe edits); classify that
            # context with the same trust patterns.
            hunk_context = "\n".join(
                context
                for line in patch.splitlines()
                if line.startswith("@@")
                and len(parts := line.split("@@")) >= 3
                and (context := parts[2].strip())
            )
            if (
                not status.startswith(("A", "M", "D"))
                or "Binary files " in patch
                or _MAKEFILE_TRUST_CHANGE.search(changed_lines)
                or _SECURITY_CHANGE.search(changed_lines)
                or _MAKEFILE_TRUST_CHANGE.search(hunk_context)
                or _SECURITY_CHANGE.search(hunk_context)
            ):
                triggered.add(path)
            continue
        python_security_change = path.endswith(".py") and (
            _OWNERSHIP_CHANGE.search(changed_lines)
            or _SECURITY_CHANGE.search(changed_lines)
        )
        if python_security_change:
            triggered.add(path)
        elif any(
            fnmatch.fnmatch(path, pattern)
            for pattern in _ALWAYS_SECURITY_REVIEW_PATTERNS
        ):
            if (
                "/integrations/" not in f"/{path}"
                or status.startswith("A")
                or _SECURITY_CHANGE.search(changed_lines)
            ):
                triggered.add(path)
        elif path.endswith((".ts", ".tsx", ".js", ".jsx")):
            current = run(
                ["git", "show", f"{head_ref}:{path}"],
                check=False,
            ).stdout
            if "use server" in current and (
                status.startswith("A") or _SERVER_ACTION_CHANGE.search(changed_lines)
            ):
                triggered.add(path)
        elif dependency_manifest:
            removed = {
                match.group(1).lower()
                for line in patch.splitlines()
                if line.startswith("-")
                and (
                    match := re.search(
                        r"[\"']?([@\w.-]+)[\"']?\s*(?::|[<>=])", line[1:]
                    )
                )
            }
            added = {
                match.group(1).lower()
                for line in patch.splitlines()
                if line.startswith("+")
                and (
                    match := re.search(
                        r"[\"']?([@\w.-]+)[\"']?\s*(?::|[<>=])", line[1:]
                    )
                )
            }
            if added - removed:
                triggered.add(path)
    return tuple(sorted(triggered))


def security_trigger_paths(
    worktree: Path,
    merge_base: str,
    *,
    runner: CommandRunner | None = None,
) -> tuple[str, ...]:
    """Evaluate canonical triggers on one frozen candidate diff."""
    return security_trigger_paths_between(
        worktree,
        merge_base,
        "HEAD",
        runner=runner,
    )


# Finding-confirmation deltas retain their existing, narrower trigger policy.
DELTA_SECURITY_PATTERNS = (
    "*dependencies*.py",
    "*middleware*",
    "*/integrations/*",
    "*config.py",
    "*.env*",
    "pyproject.toml",
    "package.json",
)
