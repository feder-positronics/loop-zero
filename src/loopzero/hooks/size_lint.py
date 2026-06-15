#!/usr/bin/env python3
"""Touched-file module-size guard (audit finding F10, issue #765).

Warn when a *touched* source file exceeds the soft threshold (800 backend lines
/ 400 frontend lines); hard-fail at 2x (1600 / 800). Only the files passed in
(pre-commit's staged set) are checked — pre-existing large files elsewhere are
never scanned, so this stops new growth without a repo-wide sweep.

Cohesive modules where decomposition is not warranted (cross-layer parsers) and
a small baseline of already-over-limit files are exempt via ALLOWLIST. The
allowlist is a ratchet: when a baselined file is decomposed below its hard
limit, remove its entry to lock in the win.

Usage (pre-commit passes staged paths as args; falls back to the staged set):
    python3 scripts/hooks/size_lint.py [path ...]

Exit codes:
    0  no file over the hard limit (warnings are non-blocking)
    1  at least one touched file over the hard limit
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND_WARN = 800
FRONTEND_WARN = 400
HARD_MULTIPLIER = 2  # hard-fail threshold = warn x 2

# Exempt from both warn and fail.
# - Parser prefixes: cohesive cross-layer XML/text parsers where splitting hurts.
# - Baseline files: already over the hard limit before this guard existed; exempt
#   to avoid blocking edits. Decompose and remove the entry to lock in the win.
ALLOWLIST_PREFIXES: tuple[str, ...] = ("fastapi_backend/app/parsers/",)
ALLOWLIST_FILES: frozenset[str] = frozenset(
    {
        # cohesive JATS XML parser
        "fastapi_backend/app/etl/fulltext/providers/pmc/jats_parser.py",
        # baseline: 2152-line one-off benchmark script; decomposition low-value
        "fastapi_backend/app/scripts/run_structured_pdf_engine_benchmark.py",
    }
)

# Not source code-debt signal — never checked.
SKIP_SUBSTRINGS: tuple[str, ...] = (
    "/__tests__/",
    "/tests/",
    ".test.",
    ".spec.",
    "openapi-client/",
    ".gen.ts",
    "/alembic_migrations/",
    "/.next/",
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    lines: int
    limit: int
    level: str  # "warn" | "fail"


def warn_threshold(path: str) -> int | None:
    """Soft threshold for a source path, or None if the path is not in scope."""
    if path.endswith(".py") and path.startswith("fastapi_backend/app/"):
        return BACKEND_WARN
    if path.endswith((".ts", ".tsx")) and path.startswith("nextjs-frontend/"):
        return FRONTEND_WARN
    return None


def is_exempt(path: str) -> bool:
    if any(token in path for token in SKIP_SUBSTRINGS):
        return True
    if path in ALLOWLIST_FILES:
        return True
    return any(path.startswith(prefix) for prefix in ALLOWLIST_PREFIXES)


def evaluate(files: dict[str, int]) -> list[Finding]:
    """Pure core: map {path: line_count} to size findings (warn/fail)."""
    findings: list[Finding] = []
    for path in sorted(files):
        warn = warn_threshold(path)
        if warn is None or is_exempt(path):
            continue
        lines = files[path]
        hard = warn * HARD_MULTIPLIER
        if lines > hard:
            findings.append(Finding(path, lines, hard, "fail"))
        elif lines > warn:
            findings.append(Finding(path, lines, warn, "warn"))
    return findings


def count_lines(path: str) -> int:
    """Count newlines, matching `wc -l` semantics."""
    return Path(path).read_bytes().count(b"\n")


def staged_paths() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    paths = argv or staged_paths()
    files: dict[str, int] = {}
    for path in paths:
        try:
            files[path] = count_lines(path)
        except (FileNotFoundError, OSError):
            continue  # deleted in this commit, or unreadable

    findings = evaluate(files)
    warnings = [f for f in findings if f.level == "warn"]
    failures = [f for f in findings if f.level == "fail"]

    for f in warnings:
        print(
            f"  ⚠ {f.path}: {f.lines} lines > {f.limit} (soft). Consider "
            f"decomposing (directory + barrel, or a concern module).",
            file=sys.stderr,
        )
    for f in failures:
        print(
            f"  ✗ {f.path}: {f.lines} lines > {f.limit} (hard limit, 2x soft). "
            f"Decompose before committing, or add to ALLOWLIST in "
            f"scripts/hooks/size_lint.py with rationale if structurally cohesive.",
            file=sys.stderr,
        )

    if failures:
        print(
            f"\nsize-lint: {len(failures)} file(s) over the hard limit "
            f"({len(warnings)} warning(s)).",
            file=sys.stderr,
        )
        return 1
    if warnings:
        print(
            f"size-lint: {len(warnings)} warning(s) (non-blocking).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
