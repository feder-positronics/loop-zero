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
TESTS_CAP=13500
CORE_DOCS_CAP=1500
DELEGATION_DOC_CAP=660
COMPLEXITY_CAP=10
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
base=${1:-$(git merge-base HEAD origin/main)}
count_base() {
  local path=$1 ext=$2
  git -c core.quotepath=off ls-tree -rz --name-only "$base" -- "$path" | { grep -z "$ext" || true; } \
    | xargs -0 -r -I{} git show "$base:{}" | wc -l
}
# The candidate is the working tree, so uncommitted and new files count too.
count_tree() {
  local path=$1 ext=$2
  git -c core.quotepath=off ls-files -zco --exclude-standard -- "$path" | { grep -z "$ext" || true; } \
    | xargs -0 -r sh -ec 'for f; do if [ -f "$f" ]; then cat -- "$f"; fi; done' _ | wc -l
}
status=0
report() { # name path cap ext_re
  local now was
  now=$(count_tree "$2" "$4"); was=$(count_base "$2" "$4")
  echo "$1=$now (base $was, delta $((now - was)), cap $3, headroom $(($3 - now)))"
  if [ "$now" -gt "$3" ] && [ "$now" -gt "$was" ]; then status=1; fi
}
report src src/loopzero "$SRC_CAP" '\.py$'
report tests tests "$TESTS_CAP" '\.py$'
report core_docs core "$CORE_DOCS_CAP" '\.md$'
report delegation_doc docs/DELEGATION.md "$DELEGATION_DOC_CAP" '\.md$'

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

"${PY[@]}" - "$base" src/loopzero "$COMPLEXITY_CAP" <<'PY' || status=1
import ast
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 14):
    sys.exit(f"size_budget: requires Python 3.14+ to parse src/loopzero, got {sys.version.split()[0]}")

base, target, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], check=True,
                          capture_output=True).stdout


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


base_scores: dict[tuple[str, str], int] = {}
for raw in git_bytes("ls-tree", "-rz", "--name-only", base, "--", target).split(b"\0"):
    if not raw or not raw.endswith(b".py"):
        continue
    rel = raw.decode("utf-8", errors="surrogateescape")
    base_scores.update(file_complexities(rel, git_bytes("show", f"{base}:{rel}")))

tree_scores: dict[tuple[str, str], int] = {}
for raw in git_bytes("ls-files", "-zco", "--exclude-standard", "--", target).split(b"\0"):
    if not raw or not raw.endswith(b".py"):
        continue
    rel = raw.decode("utf-8", errors="surrogateescape")
    p = Path(rel)
    if p.is_file():
        tree_scores.update(file_complexities(rel, p.read_bytes()))

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
PY
[ "$status" -eq 0 ] || echo "size budget: FAIL (over cap and grew against base, or complexity regressed)"
exit "$status"
