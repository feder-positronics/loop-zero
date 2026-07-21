#!/usr/bin/env bash
# Shared pre-push gate for local and agent workflows.
# Mirrors the main local-vs-CI drift checks without running the full CI suite.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

now_ms() {
    python3 -c 'import time; print(time.monotonic_ns() // 1_000_000)'
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

collect_changed_files() {
    local tmp
    tmp="$(mktemp)"

    if ! git diff --name-only "${base_ref}...HEAD" >>"$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if ! git diff --name-only >>"$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if ! git diff --cached --name-only >>"$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if ! git ls-files --others --exclude-standard >>"$tmp"; then
        rm -f "$tmp"
        return 1
    fi

    if ! sort -u "$tmp" | sed '/^$/d'; then
        rm -f "$tmp"
        return 1
    fi
    rm -f "$tmp"
}

should_run_blueprint_drift() {
    if [ "${CI_MIRROR_BLUEPRINT_DRIFT:-}" = "1" ]; then
        return 0
    fi

    has_relevant_changes_in \
        docs/design/blueprints \
        docs/policies/repo-workflow.yaml \
        scripts/docs/blueprint-activate.py \
        scripts/docs/blueprint-complete.py \
        scripts/docs/blueprint-lifecycle.py \
        scripts/docs/check_blueprint_refs.py \
        scripts/hooks/ci-mirror-check.sh
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

echo ""
echo "== Repository workflow policy =="
started_ms="$(now_ms)"
if make check-repo-workflow-policy; then
    emit_tool_event "repo-workflow-policy" "$started_ms" "pass"
else
    emit_tool_event "repo-workflow-policy" "$started_ms" "fail"
    exit 1
fi

echo ""
echo "== Docs governance: blueprint drift =="
if ! should_run_blueprint_drift; then
    echo "No blueprint-policy changes relative to ${base_ref}; skipping online blueprint drift check."
    echo "Set CI_MIRROR_BLUEPRINT_DRIFT=1 to force it locally."
    emit_tool_event "blueprint-drift" "$(now_ms)" "skip"
elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    started_ms="$(now_ms)"
    if [ "${CI_MIRROR_BLUEPRINT_DRIFT:-}" = "1" ]; then
        blueprint_cmd=(make blueprint-drift-check)
    elif has_local_changes_in docs/design/blueprints; then
        echo "Local blueprint edits found; running offline structure drift only."
        echo "Online changed-blueprint issue-state drift will run after the edits are committed."
        blueprint_cmd=(python3 scripts/docs/blueprint-drift-check.py --offline)
    elif git diff --quiet "${base_ref}...HEAD" -- docs/design/blueprints; then
        echo "No committed blueprint file changes relative to ${base_ref}; running offline structure drift only."
        blueprint_cmd=(python3 scripts/docs/blueprint-drift-check.py --offline)
    else
        blueprint_cmd=(python3 scripts/docs/blueprint-drift-check.py --changed-base "${base_ref}")
    fi
    if "${blueprint_cmd[@]}"; then
        emit_tool_event "blueprint-drift" "$started_ms" "pass"
    else
        emit_tool_event "blueprint-drift" "$started_ms" "fail"
        exit 1
    fi
else
    echo "gh is unavailable or unauthenticated; skipping online blueprint drift check."
    echo "CI will still enforce live issue-label and issue-state drift."
    emit_tool_event "blueprint-drift" "$(now_ms)" "skip"
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
        validation_scope_args=("${frontend_changed_files[@]}")
        if [ "${CI_MIRROR_FULL_FRONTEND:-}" = "1" ]; then
            validation_scope_args=(--force "${validation_scope_args[@]}")
        fi
        frontend_validation_scope="$(python3 scripts/util/frontend_validation_scope.py "${validation_scope_args[@]}")"
        if [ "$frontend_validation_scope" = "full" ]; then
            echo "Shared/broad frontend boundary changed; running the full frontend suite."
            frontend_test_cmd=(pnpm exec vitest run)
            frontend_event="frontend-full-tests"
        else
            frontend_test_cmd=(pnpm exec vitest related --run --passWithNoTests "${frontend_changed_args[@]}")
            frontend_event="frontend-changed-tests"
        fi
        if (cd nextjs-frontend && "${frontend_test_cmd[@]}"); then
            emit_tool_event "$frontend_event" "$started_ms" "pass"
        else
            emit_tool_event "$frontend_event" "$started_ms" "fail"
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
    echo "Removed from local mirror for cost. Use make test-for in the edit loop,"
    echo "targeted integration for DB/API/task paths, and PR/post-merge CI for broad coverage."
    emit_tool_event "backend-changed-tests" "$(now_ms)" "skip"
fi

changed_files_output="$(collect_changed_files)" || {
    echo "Unable to enumerate changed files for agent-config sync." >&2
    exit 1
}
changed_files=()
if [ -n "$changed_files_output" ]; then
    mapfile -t changed_files <<<"$changed_files_output"
fi
if [ ${#changed_files[@]} -eq 0 ]; then
    semantic_changes=""
else
    semantic_changes="$(
        python3 scripts/util/skill_semantic_paths.py -- "${changed_files[@]}"
    )" || exit 1
fi
if [ -z "$semantic_changes" ]; then
    echo ""
    echo "== Agent config sync =="
    echo "No convergence-trigger changes relative to ${base_ref}; skipping sync validation."
else
    echo ""
    echo "== Agent config sync =="
    echo "Convergence-trigger changes:"
    printf '%s\n' "$semantic_changes"
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
