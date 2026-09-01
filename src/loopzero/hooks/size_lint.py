#!/usr/bin/env python3
"""Touched-file module-size guard (audit finding F10, issue #765; scope #3944).

Warn when a *touched* source file exceeds its soft threshold; hard-fail at 2x.
Only the files passed in (pre-commit's staged set) are checked — pre-existing
large files elsewhere are never scanned, so this stops new growth without a
repo-wide sweep.

Thresholds (soft / hard = 2x soft):
    fastapi_backend/app/**.py           800 / 1600
    scripts/**.py                       800 / 1600
    Python test files (test_*.py or
    under fastapi_backend/tests/)      2000 / 4000
    nextjs-frontend/**.{ts,tsx}         400 /  800

Frontend test files (`.test.`, `.spec.`, `__tests__/`) stay out of scope.

Touched non-test Python files additionally get a warn-only per-function length
signal (> FUNCTION_WARN lines): a function that long cannot be read, tested,
or edited in pieces, which is worse for agent development than a large file of
small functions. It never blocks.

Cohesive modules where decomposition is not warranted (cross-layer parsers)
are exempt via COHESIVE_FILES; files already over the hard limit when their
path came into scope are exempt via BASELINE_FILES. The baseline is a
ratchet-tested list: once a file drops below its hard limit, a unit test fails
until its entry is removed, locking in the win.

Usage (pre-commit passes staged paths as args; falls back to the staged set):
    python3 scripts/hooks/size_lint.py [path ...]

Exit codes:
    0  no file over the hard limit (warnings are non-blocking)
    1  at least one touched file over the hard limit
"""

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND_WARN = 800
SCRIPTS_WARN = 800
PY_TEST_WARN = 2000
FRONTEND_WARN = 400
HARD_MULTIPLIER = 2  # hard-fail threshold = warn x 2
FUNCTION_WARN = 150  # warn-only: Python function length signal

# Exempt from both warn and fail.
# - Parser prefixes: cohesive cross-layer XML/text parsers where splitting hurts.
# - Baseline files: already over the hard limit before this guard covered their
#   path (backend baseline: pre-#765; scripts/tests baseline: pre-#3944).
#   Exempt to avoid blocking edits. Decompose and remove the entry to lock in
#   the win.
ALLOWLIST_PREFIXES: tuple[str, ...] = ("fastapi_backend/app/parsers/",)
# Cohesive by design: exempt regardless of size.
COHESIVE_FILES: frozenset[str] = frozenset(
    {
        # cohesive JATS XML parser
        "fastapi_backend/app/etl/fulltext/providers/pmc/jats_parser.py",
    }
)
# Baseline ratchet: exempt only while still over the hard limit; a test fails
# the entry once the file is decomposed below it, locking in the win.
BASELINE_FILES: frozenset[str] = frozenset(
    {
        # -- scripts baseline over 1600 lines at #3944 adoption (ratchet) --
        "scripts/deploy/railway-staging-lease.py",
        "scripts/docs/check_repo_workflow_policy.py",
        "scripts/ops/compute_monitor.py",
        "scripts/util/agent_dispatch.py",
        "scripts/util/agent_runtimes/codex.py",
        "scripts/util/agent_runtimes/sdk_bridge.py",
        "scripts/util/audit_manifest.py",
        "scripts/util/decision_review_dispatch.py",
        "scripts/util/delivery_control.py",
        "scripts/util/delivery_pipeline.py",
        "scripts/util/final_ci_gate.py",
        "scripts/util/finding_ledger.py",
        "scripts/util/guardian_delivery.py",
        "scripts/util/guardian_dispatch.py",
        "scripts/util/guardian_gate_b.py",
        "scripts/util/pr_merge_gate.py",
        "scripts/util/runtime_owner.py",
        "scripts/util/skill_convergence.py",
        # -- Python test baseline over 4000 lines at #3944 adoption (ratchet) --
        "fastapi_backend/tests/integration/api/test_agent_tasks_api.py",
        "fastapi_backend/tests/unit/agent_runner/test_intelflo_agent.py",
        "fastapi_backend/tests/unit/scripts/test_agent_dispatch.py",
        "fastapi_backend/tests/unit/scripts/test_agent_runtimes.py",
        "fastapi_backend/tests/unit/scripts/test_guardian_dispatch.py",
        "fastapi_backend/tests/unit/scripts/test_railway_staging_lease.py",
    }
)
ALLOWLIST_FILES: frozenset[str] = COHESIVE_FILES | BASELINE_FILES

# Not source code-debt signal — never checked. Frontend test markers stay here;
# Python tests are in scope with their own threshold.
SKIP_SUBSTRINGS: tuple[str, ...] = (
    "/__tests__/",
    ".test.",
    ".spec.",
    "openapi-client/",
    ".gen.ts",
    "/.next/",
    "/alembic_migrations/",
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    lines: int
    limit: int
    level: str  # "warn" | "fail"


def is_python_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.endswith(".py") and (
        name.startswith("test_") or path.startswith("fastapi_backend/tests/")
    )


def warn_threshold(path: str) -> int | None:
    """Soft threshold for a source path, or None if the path is not in scope."""
    if path.endswith(".py"):
        if is_python_test(path):
            # Any test file under a covered root, including stray test_*.py
            # under fastapi_backend/app/, gets the test threshold.
            if path.startswith(("fastapi_backend/", "scripts/")):
                return PY_TEST_WARN
            return None
        if path.startswith("fastapi_backend/app/"):
            return BACKEND_WARN
        if path.startswith("scripts/"):
            return SCRIPTS_WARN
        return None
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


def function_findings(path: str, source: str) -> list[Finding]:
    """Warn-only: functions/methods longer than FUNCTION_WARN lines."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []  # unparseable source is other hooks' problem; never block here
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        span = end - node.lineno + 1
        if span > FUNCTION_WARN:
            findings.append(
                Finding(f"{path}:{node.lineno} def {node.name}", span, FUNCTION_WARN, "warn")
            )
    return findings


def evaluate_functions(paths: list[str]) -> list[Finding]:
    """Per-function length warnings for touched, in-scope, non-test Python files."""
    findings: list[Finding] = []
    for path in sorted(paths):
        if not path.endswith(".py") or is_python_test(path):
            continue
        if warn_threshold(path) is None or is_exempt(path):
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        findings.extend(function_findings(path, source))
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
    function_warnings = evaluate_functions(list(files))

    for f in warnings:
        print(
            f"  ⚠ {f.path}: {f.lines} lines > {f.limit} (soft). Consider "
            f"decomposing (directory + barrel, or a concern module).",
            file=sys.stderr,
        )
    for f in function_warnings:
        print(
            f"  ⚠ {f.path}: {f.lines} lines > {f.limit} (function soft limit). "
            f"Consider splitting into pipeline stages or helpers.",
            file=sys.stderr,
        )
    for f in failures:
        print(
            f"  ✗ {f.path}: {f.lines} lines > {f.limit} (hard limit, 2x soft). "
            f"Decompose before committing; if structurally cohesive, add to "
            f"COHESIVE_FILES in scripts/hooks/size_lint.py with rationale "
            f"(BASELINE_FILES is reserved for the ratchet-tested backlog).",
            file=sys.stderr,
        )

    if failures:
        print(
            f"\nsize-lint: {len(failures)} file(s) over the hard limit "
            f"({len(warnings) + len(function_warnings)} warning(s)).",
            file=sys.stderr,
        )
        return 1
    if warnings or function_warnings:
        print(
            f"size-lint: {len(warnings) + len(function_warnings)} warning(s) "
            f"(non-blocking).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
