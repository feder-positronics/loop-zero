#!/usr/bin/env bash
# Shared pre-push gate for local and agent workflows.
# Mirrors the main local-vs-CI drift checks without running the full CI suite.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

# Git exports repository-local variables to hooks. If they reach a test that
# creates a nested repository, `git -C <temp-dir>` still targets this live
# worktree and can rewrite its refs, index, and config. The mirror has already
# resolved its root by path, so clear only Git's documented local variables;
# subsequent commands rediscover this repository from the working directory.
if ! git_local_env_names="$(git rev-parse --local-env-vars)"; then
    echo "✗ Could not enumerate repository-local Git hook variables." >&2
    exit 1
fi
while IFS= read -r git_env_name; do
    [ -n "$git_env_name" ] && unset "$git_env_name"
done <<< "$git_local_env_names"
unset git_local_env_names

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

should_run_test_for_oracle() {
    has_relevant_changes_in \
        Makefile \
        fastapi_backend/pyproject.toml \
        fastapi_backend/uv.lock \
        scripts/util/backend_test_lane.py \
        scripts/util/commit-auto-fix.sh \
        scripts/hooks/ci-mirror-check.sh \
        fastapi_backend/tests/unit/scripts/test_backend_test_lane.py \
        fastapi_backend/tests/unit/scripts/test_test_for_contract.py \
        fastapi_backend/tests/unit/scripts/test_commit_autofix_merge_scope.py
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

if ! has_relevant_changes_in \
    fastapi_backend \
    .github/workflows/ci.yml \
    .github/workflows/scheduled-ci.yml \
    .github/actions/critical-playwright \
    scripts/hooks/ci-mirror-check.sh; then
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

if ! has_relevant_changes_in \
    nextjs-frontend \
    .github/workflows/ci.yml \
    .github/workflows/scheduled-ci.yml \
    .github/actions/critical-playwright \
    scripts/util/frontend_validation_scope.py \
    scripts/hooks/ci-mirror-check.sh; then
    echo ""
    echo "== Frontend static checks =="
    echo "No frontend or frontend-CI changes relative to ${base_ref}; skipping static checks."
else
    echo ""
    echo "== Frontend static checks =="
    started_ms="$(now_ms)"
    if (
        cd nextjs-frontend
        pnpm run tsc
        pnpm run lint
        pnpm exec prettier --check '**/*.{js,jsx,ts,tsx,json,css,html}'
    ); then
        emit_tool_event "frontend-static" "$started_ms" "pass"
    else
        emit_tool_event "frontend-static" "$started_ms" "fail"
        exit 1
    fi
fi

if ! has_relevant_changes_in \
    nextjs-frontend \
    .github/workflows/ci.yml \
    .github/workflows/scheduled-ci.yml \
    .github/actions/critical-playwright \
    scripts/util/frontend_validation_scope.py \
    scripts/hooks/ci-mirror-check.sh; then
    echo ""
    echo "== Frontend changed-tests =="
    echo "No frontend or frontend-CI changes relative to ${base_ref}; skipping Vitest."
else
    echo ""
    echo "== Frontend changed-tests =="
    mapfile -t frontend_changed_files < <(collect_changed_frontend_files)
    frontend_force=0
    if [ "${CI_MIRROR_FULL_FRONTEND:-}" = "1" ] || has_relevant_changes_in \
        .github/workflows/ci.yml \
        .github/workflows/scheduled-ci.yml \
        .github/actions/critical-playwright \
        scripts/util/frontend_validation_scope.py \
        scripts/hooks/ci-mirror-check.sh; then
        frontend_force=1
    fi
    if [ ${#frontend_changed_files[@]} -eq 0 ] && [ "$frontend_force" = "0" ]; then
        echo "No frontend file list resolved; skipping vitest related run."
    else
        frontend_changed_args=()
        for path in "${frontend_changed_files[@]}"; do
            frontend_changed_args+=("${path#nextjs-frontend/}")
        done
        started_ms="$(now_ms)"
        validation_scope_args=("${frontend_changed_files[@]}")
        if [ "$frontend_force" = "1" ]; then
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
        if [ "$frontend_validation_scope" != "full" ]; then
            started_ms="$(now_ms)"
            if (
                cd nextjs-frontend
                shopt -s nullglob
                guardrail_tests=(
                    __tests__/unit/*guardrail*.test.ts
                    __tests__/unit/*guardrail*.test.tsx
                )
                if [ ${#guardrail_tests[@]} -eq 0 ]; then
                    echo "No frontend repository guardrail tests found." >&2
                    exit 1
                fi
                pnpm exec vitest run "${guardrail_tests[@]}"
            ); then
                emit_tool_event "frontend-guardrail-tests" "$started_ms" "pass"
            else
                emit_tool_event "frontend-guardrail-tests" "$started_ms" "fail"
                exit 1
            fi
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

echo ""
echo "== Backend impacted-test oracle =="
if ! should_run_test_for_oracle; then
    echo "No selector/toolchain policy changes relative to ${base_ref}; skipping oracle."
    emit_tool_event "backend-test-for-oracle" "$(now_ms)" "skip"
else
    started_ms="$(now_ms)"
    if make test-for-oracle; then
        emit_tool_event "backend-test-for-oracle" "$started_ms" "pass"
    else
        emit_tool_event "backend-test-for-oracle" "$started_ms" "fail"
        exit 1
    fi
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
echo "== Background jobs =="
started_ms="$(now_ms)"
if scripts/util/job.sh check; then
    echo "No unfinished background jobs."
    emit_tool_event "job-drain" "$started_ms" "pass"
else
    emit_tool_event "job-drain" "$started_ms" "fail"
    echo ""
    echo "✗ Pushing with unfinished background jobs hides their verdict." >&2
    exit 1
fi

echo ""
echo "== Wait discipline =="
# Session-scoped poll gate (#3418 P1): fails only on model-side wait loops
# newly active since this worktree's last passing gate — never on another
# agent's historical sessions. Baseline lives beside the job dir.
started_ms="$(now_ms)"
if python3 scripts/util/poll_audit.py --gate \
    --baseline-file "${INTELFLO_JOB_DIR:-.pid/jobs}/../poll-gate-baseline" \
    --worktree "$(pwd)" \
    --days 7; then
    emit_tool_event "poll-gate" "$started_ms" "pass"
else
    emit_tool_event "poll-gate" "$started_ms" "fail"
    echo ""
    echo "✗ Model-side wait loop detected in this window (ops.mdc wait contract):" >&2
    echo "  route external waits through job.sh wait / wait-file with a p90 deadman." >&2
    exit 1
fi

echo ""
echo "✅ CI mirror check passed"
