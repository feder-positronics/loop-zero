#!/usr/bin/env bash
# Size and complexity budget shared by CI and `loopzero check`. Caps are absolute.
# A line budget over its cap fails only if it also grew against its base, and a
# function over the complexity cap fails only if it exceeds its base complexity,
# so a repository that is already over can still merge changes that hold or shrink
# and never deadlocks.
# Usage: scripts/size_budget.sh [BASE]   (default: merge-base with origin/main)
set -euo pipefail
# Measured 2026-11-10: 6777 source / 11106 test lines.
# Measured 2026-11-25: 1406 core Markdown / 600 delegation Markdown lines.
# Allow ~10% growth, rounded up, so small fixes do not require cap changes.
SRC_CAP=7500
TESTS_CAP=12300
CORE_DOCS_CAP=1500
DELEGATION_DOC_CAP=660
COMPLEXITY_CAP=10
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
python3 - "$base" src/loopzero "$COMPLEXITY_CAP" <<'PY' || status=1
import ast
import subprocess
import sys
from pathlib import Path

base, target, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], check=True,
                          capture_output=True).stdout


def fn_complexity(node: ast.AST) -> int:
    total = 1

    def walk(cur: ast.AST) -> None:
        nonlocal total
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While,
                                  ast.ExceptHandler, ast.With, ast.AsyncWith)):
                total += 1
            elif isinstance(child, (ast.Try, ast.TryStar)):
                if child.orelse:
                    total += 1
            elif isinstance(child, ast.comprehension):
                total += len(child.ifs) + 1
            elif isinstance(child, ast.Match):
                total += len(child.cases)
            walk(child)

    walk(node)
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
                cc = fn_complexity(child)
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
