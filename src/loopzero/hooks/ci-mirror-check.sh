#!/usr/bin/env bash
# Shared pre-push gate for local and agent workflows.
# Mirrors the main local-vs-CI drift checks without running the full CI suite.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

now_ms() {
    date +%s%3N
}

emit_tool_event() {
    local category="$1"
    local started_ms="$2"
    local result="$3"
    local ended_ms
    local duration_ms

    ended_ms="$(now_ms)"
    duration_ms=$((ended_ms - started_ms))
    python3 scripts/util/agent_event.py tool \
        --skill ci-mirror-check \
        --category "$category" \
        --duration-ms "$duration_ms" \
        --result "$result" >/dev/null 2>&1 || true
}

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
started_ms="$(now_ms)"
if make check-actions-branch-policy; then
    emit_tool_event "branch-policy" "$started_ms" "pass"
else
    emit_tool_event "branch-policy" "$started_ms" "fail"
    exit 1
fi

if ! has_relevant_changes_in fastapi_backend .github/workflows/ci.yml scripts/hooks/ci-mirror-check.sh; then
    echo ""
    echo "== Backend lint =="
    echo "No backend changes relative to ${base_ref}; skipping backend lint."
    echo ""
    echo "== Backend type check =="
    echo "No backend changes relative to ${base_ref}; skipping backend mypy."
else
    echo ""
    echo "== Backend lint =="
    started_ms="$(now_ms)"
    if (cd fastapi_backend && uv run ruff check .); then
        emit_tool_event "backend-ruff" "$started_ms" "pass"
    else
        emit_tool_event "backend-ruff" "$started_ms" "fail"
        exit 1
    fi

    echo ""
    echo "== Backend type check =="
    started_ms="$(now_ms)"
    if (cd fastapi_backend && uv run mypy app/); then
        emit_tool_event "backend-mypy" "$started_ms" "pass"
    else
        emit_tool_event "backend-mypy" "$started_ms" "fail"
        exit 1
    fi
fi

if ! has_relevant_changes_in nextjs-frontend .github/workflows/ci.yml scripts/hooks/ci-mirror-check.sh; then
    echo ""
    echo "== Frontend type check =="
    echo "No frontend changes relative to ${base_ref}; skipping frontend tsc."
else
    echo ""
    echo "== Frontend type check =="
    started_ms="$(now_ms)"
    if (cd nextjs-frontend && pnpm run tsc); then
        emit_tool_event "frontend-tsc" "$started_ms" "pass"
    else
        emit_tool_event "frontend-tsc" "$started_ms" "fail"
        exit 1
    fi
fi

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
        started_ms="$(now_ms)"
        if (cd nextjs-frontend && pnpm exec vitest related --run --passWithNoTests "${frontend_changed_args[@]}"); then
            emit_tool_event "frontend-changed-tests" "$started_ms" "pass"
        else
            emit_tool_event "frontend-changed-tests" "$started_ms" "fail"
            exit 1
        fi
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
    started_ms="$(now_ms)"
    if (cd fastapi_backend && uv run pytest --testmon -q -m "not slow" tests/unit tests/integration); then
        emit_tool_event "backend-changed-tests" "$started_ms" "pass"
    else
        emit_tool_event "backend-changed-tests" "$started_ms" "fail"
        exit 1
    fi
fi

if ! has_relevant_changes_in AGENTS.md .cursor .agents .agent .claude; then
    echo ""
    echo "== Agent config sync =="
    echo "No agent-surface changes relative to ${base_ref}; skipping sync validation."
else
    echo ""
    echo "== Agent config sync =="
    started_ms="$(now_ms)"
    if make check-agent-configs; then
        emit_tool_event "agent-config-sync" "$started_ms" "pass"
    else
        emit_tool_event "agent-config-sync" "$started_ms" "fail"
        exit 1
    fi
fi

echo ""
echo "✅ CI mirror check passed"
