#!/usr/bin/env bash
# Shared pre-push gate for local and agent workflows.
# Mirrors the main local-vs-CI drift checks without running the full CI suite.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

resolve_base_ref() {
    if [ -n "${CI_MIRROR_BASE_REF:-}" ]; then
        echo "$CI_MIRROR_BASE_REF"
        return
    fi

    if git rev-parse --verify --quiet origin/main >/dev/null; then
        echo "origin/main"
        return
    fi

    if git rev-parse --verify --quiet main >/dev/null; then
        echo "main"
        return
    fi

    if git rev-parse --verify --quiet HEAD~1 >/dev/null; then
        echo "HEAD~1"
        return
    fi

    echo "HEAD"
}

base_ref="$(resolve_base_ref)"

has_local_changes_in() {
    if ! git diff --quiet -- "$@"; then
        return 0
    fi

    if ! git diff --cached --quiet -- "$@"; then
        return 0
    fi

    if git ls-files --others --exclude-standard -- "$@" | grep -q .; then
        return 0
    fi

    return 1
}

has_relevant_changes_in() {
    if has_local_changes_in "$@"; then
        return 0
    fi

    ! git diff --quiet "${base_ref}...HEAD" -- "$@"
}

collect_changed_frontend_files() {
    local tmp
    tmp="$(mktemp)"

    git diff --name-only "${base_ref}...HEAD" -- nextjs-frontend >>"$tmp" || true
    git diff --name-only -- nextjs-frontend >>"$tmp"
    git diff --cached --name-only -- nextjs-frontend >>"$tmp"
    git ls-files --others --exclude-standard -- nextjs-frontend >>"$tmp"

    sort -u "$tmp" | sed '/^$/d'
    rm -f "$tmp"
}

echo "🔍 CI mirror check"
echo "   Base ref: ${base_ref}"

echo ""
echo "== GitHub Actions branch policy =="
make check-actions-branch-policy

echo ""
echo "== Backend lint =="
(cd fastapi_backend && uv run ruff check .)

echo ""
echo "== Backend type check =="
(cd fastapi_backend && uv run mypy app/)

echo ""
echo "== Frontend type check =="
(cd nextjs-frontend && pnpm run tsc)

if ! has_relevant_changes_in nextjs-frontend; then
    echo ""
    echo "== Frontend changed-tests =="
    echo "No frontend changes relative to ${base_ref}; skipping vitest related run."
else
    echo ""
    echo "== Frontend changed-tests =="
    mapfile -t frontend_changed_files < <(collect_changed_frontend_files)
    if [ ${#frontend_changed_files[@]} -eq 0 ]; then
        echo "No frontend file list resolved; skipping vitest related run."
    else
        frontend_changed_args=()
        for path in "${frontend_changed_files[@]}"; do
            frontend_changed_args+=("${path#nextjs-frontend/}")
        done
        (cd nextjs-frontend && pnpm exec vitest related --run --passWithNoTests "${frontend_changed_args[@]}")
    fi
fi

if ! has_relevant_changes_in fastapi_backend; then
    echo ""
    echo "== Backend changed-tests =="
    echo "No backend changes relative to ${base_ref}; skipping pytest --testmon run."
else
    echo ""
    echo "== Backend changed-tests =="
    # pytest-testmon uses import-graph data in .testmondata to run only the
    # tests affected by changed code. First run on a fresh checkout instruments
    # the suite (slow); subsequent runs are fast. Mirrors the frontend
    # `vitest related` pattern.
    (cd fastapi_backend && uv run pytest --testmon -q -m "not slow" tests/unit tests/integration)
fi

if ! has_relevant_changes_in AGENTS.md .cursor .agents .agent .claude; then
    echo ""
    echo "== Agent config sync =="
    echo "No agent-surface changes relative to ${base_ref}; skipping sync validation."
else
    echo ""
    echo "== Agent config sync =="
    make check-agent-configs
fi

echo ""
echo "✅ CI mirror check passed"
