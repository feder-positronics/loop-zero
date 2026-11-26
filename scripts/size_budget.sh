#!/usr/bin/env bash
# Size and complexity budget shared by CI and `loopzero check`. Caps are absolute.
# A line budget over its cap fails only if it also grew against its base, and an
# existing function over the complexity cap fails only if it exceeds its base
# complexity, so a repository that is already over can still merge changes that
# hold or shrink and never deadlocks. Functions are keyed by (path, qualname), so
# moving or renaming an over-cap function treats it as new and requires bringing
# it under the cap.
# Usage: scripts/size_budget.sh [BASE]   (default: merge-base with origin/main)
set -euo pipefail
# Measured 2026-11-26: 7494 source lines.
# Measured 2026-11-25: 12289 test lines.
# Measured 2026-11-25: 1406 core Markdown / 600 delegation Markdown lines.
# Allow ~10% growth, rounded up, so small fixes do not require cap changes.
SRC_CAP=8250
TESTS_CAP=13850
CORE_DOCS_CAP=1500
DELEGATION_DOC_CAP=660
COMPLEXITY_CAP=10
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
base=${1:-$(git merge-base HEAD origin/main)}
status=0

# Parse src/loopzero with the repository's pinned Python 3.14+ interpreter (PEP 758 syntax).
if [ -n "${PYTHON:-}" ]; then
  PY=("$PYTHON")
elif [ -x "$SCRIPT_DIR/../.venv/bin/python" ]; then
  PY=("$SCRIPT_DIR/../.venv/bin/python")
elif command -v uv >/dev/null 2>&1; then
  PY=(uv run --python 3.14 python)
else
  PY=(python3)
fi

"${PY[@]}" - "$base" "$COMPLEXITY_CAP" "$SRC_CAP" "$TESTS_CAP" "$CORE_DOCS_CAP" "$DELEGATION_DOC_CAP" <<'PY' || status=1
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

base, cap = sys.argv[1], int(sys.argv[2])
budgets = (
    ("src", b"src/loopzero", b".py"),
    ("tests", b"tests", b".py"),
    ("core_docs", b"core", b".md"),
    ("delegation_doc", b"docs/DELEGATION.md", b".md"),
)
paths = [os.fsdecode(root) for _, root, _ in budgets]


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], check=True,
                          capture_output=True).stdout


def budget_index(path: bytes) -> int | None:
    for index, (_, root, suffix) in enumerate(budgets):
        if path.endswith(suffix) and (path == root or path.startswith(root + b"/")):
            return index
    return None


def base_blobs():
    entries = git_bytes("ls-tree", "-rz", base, "--", *paths).split(b"\0")
    with subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE) as batch:
        for raw in filter(None, entries):
            metadata, path = raw.split(b"\t", 1)
            index = budget_index(path)
            if index is None:
                continue
            oid = metadata.rsplit(b" ", 1)[1]
            batch.stdin.write(oid + b"\n")
            batch.stdin.flush()
            header = batch.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise RuntimeError(f"cannot read base blob for {os.fsdecode(path)!r}: {header!r}")
            size = int(header[2])
            blob = batch.stdout.read(size)
            if len(blob) != size or batch.stdout.read(1) != b"\n":
                raise RuntimeError(f"incomplete base blob for {os.fsdecode(path)!r}")
            yield index, os.fsdecode(path), blob
        batch.stdin.close()
        if batch.wait():
            raise subprocess.CalledProcessError(batch.returncode, batch.args)


def count_stmts(stmts: list[ast.stmt]) -> int:
    """Statement-level McCabe complexity matching PyCQA/Ruff C901."""
    total = 0
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            total += 1 + count_stmts(stmt.body) + count_stmts(stmt.orelse)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            total += 1 + int(bool(stmt.orelse)) + count_stmts(stmt.body) + count_stmts(stmt.orelse)
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            total += len(stmt.handlers) + int(bool(stmt.orelse))
            total += count_stmts(stmt.body) + count_stmts(stmt.orelse) + count_stmts(stmt.finalbody)
            for handler in stmt.handlers:
                total += count_stmts(handler.body)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            total += count_stmts(stmt.body)
        elif isinstance(stmt, ast.Match):
            total += len(stmt.cases)
            for case in stmt.cases:
                total += count_stmts(case.body)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            total += 1 + count_stmts(stmt.body)
        elif isinstance(stmt, ast.ClassDef):
            total += count_stmts(stmt.body)
    return total


def file_complexities(path: str, source: bytes) -> dict[tuple[str, str], int]:
    tree = ast.parse(source, filename=path)
    scores: dict[tuple[str, str], int] = {}

    def collect(cur: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                collect(child, name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                key = (path, name)
                cc = 1 + count_stmts(child.body)
                scores[key] = max(scores.get(key, 0), cc)
                collect(child, f"{name}.<locals>")
            else:
                collect(child, prefix)

    collect(tree)
    return scores


base_counts = [0] * len(budgets)
base_scores: dict[tuple[str, str], int] = {}
parse_error = None
can_parse = sys.version_info >= (3, 14)
for index, rel, blob in base_blobs():
    base_counts[index] += blob.count(b"\n")
    if index == 0 and can_parse and parse_error is None:
        try:
            base_scores.update(file_complexities(rel, blob))
        except SyntaxError as exc:
            parse_error = exc

# Match the previous wc -l counts, including untracked files and incomplete lines.
tree_counts = [0] * len(budgets)
source_paths = []
for raw in git_bytes("ls-files", "-zco", "--exclude-standard", "--", *paths).split(b"\0"):
    index = budget_index(raw)
    if index is None:
        continue
    path = Path(os.fsdecode(raw))
    if path.is_file():
        with path.open("rb") as stream:
            tree_counts[index] += sum(block.count(b"\n") for block in iter(lambda: stream.read(65536), b""))
        if index == 0:
            source_paths.append(path)

over_budget = False
for (name, _, _), now, was, limit in zip(budgets, tree_counts, base_counts, map(int, sys.argv[3:])):
    print(f"{name}={now} (base {was}, delta {now - was}, cap {limit}, headroom {limit - now})")
    over_budget |= now > limit and now > was

if not can_parse:
    sys.exit(f"size_budget: requires Python 3.14+ to parse src/loopzero, got {sys.version.split()[0]}")
if parse_error is not None:
    raise parse_error

tree_scores: dict[tuple[str, str], int] = {}
for path in source_paths:
    tree_scores.update(file_complexities(str(path), path.read_bytes()))

regressions = []
for key, now in sorted(tree_scores.items()):
    was = base_scores.get(key, 0)
    if now > cap and now > was:
        regressions.append((key[0], key[1], now, was))

over_cap = sum(1 for v in tree_scores.values() if v > cap)
if regressions:
    print(f"complexity=FAIL (cap {cap}, over_cap {over_cap}, regressions {len(regressions)})")
    for path, qualname, now, was in regressions:
        print(f"  {path}:{qualname}={now} (base {was}, cap {cap})")
    sys.exit(1)
print(f"complexity=PASS (cap {cap}, over_cap {over_cap}, regressions 0)")
sys.exit(over_budget)
PY
[ "$status" -eq 0 ] || echo "size budget: FAIL (over cap and grew against base, or complexity regressed)"
exit "$status"
