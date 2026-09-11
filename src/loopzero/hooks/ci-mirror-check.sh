#!/usr/bin/env bash
# Shared pre-push gate for local and agent workflows.
# Mirrors the main local-vs-CI drift checks without running the full CI suite.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../kernel/settings.sh"
# TODO(A4): approved consumer wiring supplies lane roots and helper commands.
BACKEND_ROOT="${LOOPZERO_BACKEND_ROOT:-backend}"
FRONTEND_ROOT="${LOOPZERO_FRONTEND_ROOT:-frontend}"

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
    "${LOOPZERO_PYTHON:-python3}" -m loopzero.kernel.events tool \
        --skill ci-mirror-check \
        --category "$category" \
        --duration-ms "$duration_ms" \
        --result "$result" >/dev/null 2>&1 || true
}

observe_validation_receipt() {
    local category="$1"
    local result="$2"
    local expected_digest="$3"
    if [ -z "$expected_digest" ]; then
        echo "validation-receipt: pre-validation identity unavailable; observation skipped" >&2
        return
    fi
    loopzero_consumer CI_MIRROR_RECEIPTS_HOOK \
        --category "$category" \
        --base-ref "$base_ref" \
        --result "$result" \
        --expected-digest "$expected_digest" || \
        echo "validation-receipt: observation unavailable; validation still ran" >&2
}

capture_validation_receipt_identity() {
    local category="$1"
    loopzero_consumer CI_MIRROR_RECEIPTS_HOOK \
        --category "$category" \
        --base-ref "$base_ref" \
        --capture-digest 2>/dev/null || true
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

    git diff --name-only "${base_ref}...HEAD" -- "$FRONTEND_ROOT" >>"$tmp" || true
    git diff --name-only -- "$FRONTEND_ROOT" >>"$tmp"
    git diff --cached --name-only -- "$FRONTEND_ROOT" >>"$tmp"
    git ls-files --others --exclude-standard -- "$FRONTEND_ROOT" >>"$tmp"

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
if loopzero_consumer ACTIONS_BRANCH_POLICY_HOOK; then
    emit_tool_event "branch-policy" "$started_ms" "pass"
else
    emit_tool_event "branch-policy" "$started_ms" "fail"
    exit 1
fi

echo ""
echo "== Repository workflow policy =="
started_ms="$(now_ms)"
if loopzero_consumer REPO_WORKFLOW_POLICY_HOOK; then
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
        blueprint_cmd=(loopzero_consumer BLUEPRINT_DRIFT_HOOK)
    elif has_local_changes_in docs/design/blueprints; then
        echo "Local blueprint edits found; running offline structure drift only."
        echo "Online changed-blueprint issue-state drift will run after the edits are committed."
        blueprint_cmd=(loopzero_consumer BLUEPRINT_DRIFT_HOOK --offline)
    elif git diff --quiet "${base_ref}...HEAD" -- docs/design/blueprints; then
        echo "No committed blueprint file changes relative to ${base_ref}; running offline structure drift only."
        blueprint_cmd=(loopzero_consumer BLUEPRINT_DRIFT_HOOK --offline)
    else
        blueprint_cmd=(loopzero_consumer BLUEPRINT_DRIFT_HOOK --changed-base "${base_ref}")
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
    "$BACKEND_ROOT" \
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
    echo "== Backend complexity ratchet =="
    echo "Local comparison uses ${base_ref}; hosted CI remains authoritative for the immutable PR base SHA."
    started_ms="$(now_ms)"
    if loopzero_consumer COMPLEXITY_HOOK BASE_SHA="$base_ref"; then
        emit_tool_event "backend-complexity" "$started_ms" "pass"
    else
        emit_tool_event "backend-complexity" "$started_ms" "fail"
        exit 1
    fi

    echo ""
    echo "== Backend lint =="
    started_ms="$(now_ms)"
    if (cd "$BACKEND_ROOT" && loopzero_consumer BACKEND_LINT_HOOK); then
        emit_tool_event "backend-ruff" "$started_ms" "pass"
    else
        emit_tool_event "backend-ruff" "$started_ms" "fail"
        exit 1
    fi

    echo ""
    echo "== Backend type check =="
    started_ms="$(now_ms)"
    if (cd "$BACKEND_ROOT" && loopzero_consumer BACKEND_TYPES_HOOK); then
        emit_tool_event "backend-mypy" "$started_ms" "pass"
    else
        emit_tool_event "backend-mypy" "$started_ms" "fail"
        exit 1
    fi
fi

if ! has_relevant_changes_in \
    "$FRONTEND_ROOT" \
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
    frontend_static_receipt_digest="$(capture_validation_receipt_identity "frontend-static")"
    started_ms="$(now_ms)"
    if (
        cd "$FRONTEND_ROOT" &&
            loopzero_consumer FRONTEND_TYPES_HOOK &&
            loopzero_consumer FRONTEND_LINT_HOOK &&
            loopzero_consumer FRONTEND_FORMAT_HOOK '**/*.{js,jsx,ts,tsx,json,css,html}'
    ); then
        observe_validation_receipt "frontend-static" "pass" "$frontend_static_receipt_digest"
        emit_tool_event "frontend-static" "$started_ms" "pass"
    else
        observe_validation_receipt "frontend-static" "fail" "$frontend_static_receipt_digest"
        emit_tool_event "frontend-static" "$started_ms" "fail"
        exit 1
    fi
fi

if ! has_relevant_changes_in \
    "$FRONTEND_ROOT" \
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
            frontend_changed_args+=("${path#"$FRONTEND_ROOT"/}")
        done
        started_ms="$(now_ms)"
        validation_scope_args=("${frontend_changed_files[@]}")
        if [ "$frontend_force" = "1" ]; then
            validation_scope_args=(--force "${validation_scope_args[@]}")
        fi
        frontend_validation_scope="$(loopzero_consumer FRONTEND_VALIDATION_SCOPE_HOOK "${validation_scope_args[@]}")"
        if [ "$frontend_validation_scope" = "full" ]; then
            echo "Shared/broad frontend boundary changed; running the full frontend suite."
            frontend_test_cmd=(loopzero_consumer FRONTEND_TESTS_HOOK run)
            frontend_event="frontend-full-tests"
            frontend_test_receipt_digest="$(capture_validation_receipt_identity "$frontend_event")"
        else
            frontend_test_cmd=(loopzero_consumer FRONTEND_TESTS_HOOK related --run --passWithNoTests "${frontend_changed_args[@]}")
            frontend_event="frontend-changed-tests"
            frontend_test_receipt_digest=""
        fi
        if (cd "$FRONTEND_ROOT" && "${frontend_test_cmd[@]}"); then
            if [ "$frontend_event" = "frontend-full-tests" ]; then
                observe_validation_receipt "$frontend_event" "pass" "$frontend_test_receipt_digest"
            fi
            emit_tool_event "$frontend_event" "$started_ms" "pass"
        else
            if [ "$frontend_event" = "frontend-full-tests" ]; then
                observe_validation_receipt "$frontend_event" "fail" "$frontend_test_receipt_digest"
            fi
            emit_tool_event "$frontend_event" "$started_ms" "fail"
            exit 1
        fi
        if [ "$frontend_validation_scope" != "full" ]; then
            started_ms="$(now_ms)"
            if (
                cd "$FRONTEND_ROOT"
                shopt -s nullglob
                guardrail_tests=(
                    __tests__/unit/*guardrail*.test.ts
                    __tests__/unit/*guardrail*.test.tsx
                )
                if [ ${#guardrail_tests[@]} -eq 0 ]; then
                    echo "No frontend repository guardrail tests found." >&2
                    exit 1
                fi
                loopzero_consumer FRONTEND_TESTS_HOOK run "${guardrail_tests[@]}"
            ); then
                emit_tool_event "frontend-guardrail-tests" "$started_ms" "pass"
            else
                emit_tool_event "frontend-guardrail-tests" "$started_ms" "fail"
                exit 1
            fi
        fi
    fi
fi

if ! has_relevant_changes_in "$BACKEND_ROOT"; then
    echo ""
    echo "== Backend changed-tests =="
    echo "No backend changes relative to ${base_ref}; skipping backend changed-tests."
else
    echo ""
    echo "== Backend changed-tests =="
    echo "Removed from local mirror for cost. Use the focused owning test in the edit loop,"
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
        loopzero_consumer SKILL_SEMANTIC_PATHS_HOOK -- "${changed_files[@]}"
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
    agent_config_receipt_digest="$(capture_validation_receipt_identity "agent-config-sync")"
    started_ms="$(now_ms)"
    if loopzero_consumer AGENT_CONFIGS_HOOK; then
        observe_validation_receipt "agent-config-sync" "pass" "$agent_config_receipt_digest"
        emit_tool_event "agent-config-sync" "$started_ms" "pass"
    else
        observe_validation_receipt "agent-config-sync" "fail" "$agent_config_receipt_digest"
        emit_tool_event "agent-config-sync" "$started_ms" "fail"
        exit 1
    fi
fi

echo ""
echo "== Skill-convergence validator tests =="
if ! has_relevant_changes_in \
    scripts/util/skill_convergence.py \
    scripts/util/test_skill_convergence.py \
    scripts/util/skill_semantic_paths.py \
    scripts/util/test_skill_semantic_paths.py; then
    echo "No validator source changes relative to ${base_ref}; skipping validator unit tests."
    emit_tool_event "skill-convergence-tests" "$(now_ms)" "skip"
else
    started_ms="$(now_ms)"
    if loopzero_consumer SKILL_CONVERGENCE_TESTS_HOOK; then
        emit_tool_event "skill-convergence-tests" "$started_ms" "pass"
    else
        emit_tool_event "skill-convergence-tests" "$started_ms" "fail"
        exit 1
    fi
fi

echo ""
echo "== Background jobs =="
started_ms="$(now_ms)"
if "$(dirname "${BASH_SOURCE[0]}")/../kernel/job.sh" check; then
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
# The first agent push starts at its logical-run boundary; later clean pushes
# advance the existing baseline. Human pushes use the same baseline without
# requiring an agent lifecycle. Canonical runtime identity lives in agent_event.
started_ms="$(now_ms)"
skill_runs_dir="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.audit/skill-runs"
if ! runtime_kind="$("${LOOPZERO_PYTHON:-python3}" -m loopzero.kernel.events runtime-kind)"; then
    emit_tool_event "poll-gate" "$started_ms" "fail"
    echo "✗ Cannot classify this push as an agent or human runtime." >&2
    exit 1
fi
git_branch="$(git symbolic-ref --quiet --short HEAD || true)"
if [ -z "$git_branch" ]; then
    emit_tool_event "poll-gate" "$started_ms" "fail"
    echo "✗ Wait-discipline checks require a named branch; detached HEAD is unsupported." >&2
    exit 1
fi
if loopzero_consumer POLL_AUDIT_HOOK --gate \
    --baseline-file "$(loopzero_env JOB_DIR '.pid/jobs')/../poll-gate-baseline" \
    --worktree "$(pwd)" \
    --skill-runs-dir "$skill_runs_dir" \
    --git-branch "$git_branch" \
    --caller-kind "$runtime_kind" \
    --days 7; then
    emit_tool_event "poll-gate" "$started_ms" "pass"
else
    emit_tool_event "poll-gate" "$started_ms" "fail"
    echo ""
    echo "✗ Wait discipline or agent lifecycle ownership failed (ops.mdc):" >&2
    echo "  route external waits through job.sh wait / wait-file with a p90 deadman." >&2
    exit 1
fi

# Advisory push-batching signal (#4014): each ready-PR push fires ~3 hosted
# workflow runs and invalidates the per-SHA final-CI verdict. Local counter
# only; never changes the hook's exit status.
push_count_root="$(loopzero_env JOB_DIR '.pid/jobs')/../push-counts"
if mkdir -p "$push_count_root" 2>/dev/null; then
    push_count_file="$push_count_root/$(printf '%s' "$git_branch" | tr -c 'A-Za-z0-9._-' '_')"
    previous_pushes="$(cat "$push_count_file" 2>/dev/null || true)"
    case "$previous_pushes" in
    '' | *[!0-9]*) previous_pushes=0 ;;
    esac
    push_count=$((10#$previous_pushes + 1))
    printf '%s\n' "$push_count" >"$push_count_file" 2>/dev/null || true
    if [ "$push_count" -ge 3 ]; then
        echo ""
        echo "ℹ Pre-push mirror run #$push_count for '$git_branch' on this checkout. Each ready-PR push"
        echo "  fires ~3 hosted workflow runs and invalidates the per-SHA ci-final verdict;"
        echo "  batch review and CI fixes into ONE push per round (branch-workflow.mdc)."
    fi
fi

echo ""
echo "✅ CI mirror check passed"
