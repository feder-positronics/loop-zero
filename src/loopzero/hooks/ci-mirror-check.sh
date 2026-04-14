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

echo "🔍 CI mirror check"
echo "   Base ref: ${base_ref}"

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
    echo "No frontend changes relative to ${base_ref}; skipping Jest changedSince run."
else
    echo ""
    echo "== Frontend changed-tests =="
    (cd nextjs-frontend && pnpm test -- --changedSince="${base_ref}" --passWithNoTests)
fi

if ! has_relevant_changes_in AGENTS.md .cursor .agents .agent .claude; then
    echo ""
    echo "== Agent config sync =="
    echo "No agent-surface changes relative to ${base_ref}; skipping sync validation."
else
    echo ""
    echo "== Agent config sync =="
    make sync-agent-configs
fi

echo ""
echo "✅ CI mirror check passed"
