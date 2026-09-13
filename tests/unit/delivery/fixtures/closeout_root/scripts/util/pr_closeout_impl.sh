#!/bin/bash
# Private shell implementation for pr_closeout.py. Invoke the Python launcher;
# it enters Bash privileged mode and scrubs startup/transport variables before
# this file is parsed.
#
# PR close-out: the mechanical tail of work-issue Phase 9 / execute-blueprint
# Step 6, made deterministic. Safe to run from a task worktree — every
# durable state is anchored in the PRIMARY checkout so it survives worktree
# removal. Helpers execute from this delivery checkout to keep their CLI
# contracts consistent; the audit logger resolves the shared git directory.
#
# Usage: /usr/bin/python3 -I /absolute/primary/scripts/util/pr_closeout.py
#        --pr <num> --skill <name> [options]
#
#   --pr <num>            PR number (required)
#   --skill <name>        skill emitting the run log (required)
#   --issue <num>         linked issue; verifies it closed
#   --merge               ensure one complete final-CI verdict for the current
#                         head unless all paths are docs/** or *.md, then merge
#                         through scripts/util/pr_merge_gate.py:
#                         serialized lock, conditional base freshness (strict
#                         up-to-date only for full-suite risk PRs; behind-base
#                         OK when changed paths don't overlap), green checks,
#                         backend full-suite check when backend paths changed;
#                         branch deletion happens explicitly afterwards
#   --blueprint <slug>    lifecycle-owned blueprint to verify before merge
#   --outcome <o>         run-log outcome (default: merged)
#   --run-id <id>         logical run ID; otherwise read the PR body marker
#   --merge-delta-review-task <id> governed passing review for a relevant merge delta
#   --merge-delta-review-run-id <id> fresh run that owns a frozen-capsule recovery review
#   --origin-url <url>    exact GitHub HTTPS remote for every Git network call
#   --git-config-sha256   security projection of .git/config checked before Git network
#   --review-passes <N>   forwarded to skill_run_log.py
#   --notes <text>        forwarded to skill_run_log.py (≤100 chars)
#
# Encodes the failure modes from .audit/work-issue-ideas.md: remote merge
# succeeding while local cleanup fails, local branch deletion failing in
# multi-worktree setups, atomic blueprint completion, run-log loss.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log_info()  { echo -e "${GREEN}✓${NC} $1"; }
log_warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }

PR="" SKILL="" ISSUE="" BLUEPRINT="" OUTCOME="merged" REVIEW_PASSES="" NOTES="" RUN_ID="" MERGE_DELTA_REVIEW_TASK="" MERGE_DELTA_REVIEW_RUN_ID="" ORIGIN_URL="" GIT_CONFIG_SHA256=""
RUN_ID_EXPLICIT=0
DO_MERGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --pr) PR="$2"; shift 2 ;;
        --skill) SKILL="$2"; shift 2 ;;
        --issue) ISSUE="$2"; shift 2 ;;
        --merge) DO_MERGE=1; shift ;;
        --blueprint) BLUEPRINT="$2"; shift 2 ;;
        --outcome) OUTCOME="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; RUN_ID_EXPLICIT=1; shift 2 ;;
        --merge-delta-review-task) MERGE_DELTA_REVIEW_TASK="$2"; shift 2 ;;
        --merge-delta-review-run-id) MERGE_DELTA_REVIEW_RUN_ID="$2"; shift 2 ;;
        --origin-url) ORIGIN_URL="$2"; shift 2 ;;
        --git-config-sha256) GIT_CONFIG_SHA256="$2"; shift 2 ;;
        --review-passes) REVIEW_PASSES="$2"; shift 2 ;;
        --notes) NOTES="$2"; shift 2 ;;
        *) log_error "Unknown argument: $1"; exit 2 ;;
    esac
done
[ -n "$PR" ] && [ -n "$SKILL" ] || { log_error "--pr and --skill are required"; exit 2; }
[[ "$PR" =~ ^[1-9][0-9]*$ ]] || {
    log_error "--pr must be a positive integer"
    exit 2
}
if [ "$OUTCOME" != "merged" ] && [ "$OUTCOME" != "in_progress" ]; then
    log_error "--outcome must be merged or the bounded in_progress exception"
    exit 2
fi
if [ "$RUN_ID_EXPLICIT" -eq 1 ] && ! [[ "$RUN_ID" =~ ^sr_[0-9a-f]{32}$ ]]; then
    log_error "--run-id must match sr_<32 lowercase hex characters>"
    exit 2
fi
if [ -n "$MERGE_DELTA_REVIEW_RUN_ID" ] && [ -z "$MERGE_DELTA_REVIEW_TASK" ]; then
    log_error "--merge-delta-review-run-id requires --merge-delta-review-task"
    exit 2
fi
if [ -n "$MERGE_DELTA_REVIEW_RUN_ID" ] \
    && ! [[ "$MERGE_DELTA_REVIEW_RUN_ID" =~ ^sr_[0-9a-f]{32}$ ]]; then
    log_error "--merge-delta-review-run-id must match sr_<32 lowercase hex characters>"
    exit 2
fi
if [ -n "$MERGE_DELTA_REVIEW_RUN_ID" ] && [ "$RUN_ID_EXPLICIT" -ne 1 ]; then
    log_error "cross-run merge-delta review requires explicit --run-id recovery"
    exit 2
fi
# This option selects review authority only; credential composition stays in
# the separately pinned Git transport boundary below.
if { [ -n "$ORIGIN_URL" ] && [ -z "$GIT_CONFIG_SHA256" ]; } \
    || { [ -z "$ORIGIN_URL" ] && [ -n "$GIT_CONFIG_SHA256" ]; }; then
    log_error "--origin-url and --git-config-sha256 must be supplied together"
    exit 2
fi

# Remove inherited execution and transport controls before the first Git or
# GitHub command. Authentication variables remain available to `gh`; the
# repository and credential-helper bindings below are reconstructed locally.
unsafe_network_vars=(
    BASH_ENV CDPATH CURL_CA_BUNDLE ENV GH_REPO
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_ASKPASS GIT_CEILING_DIRECTORIES
    GIT_COMMON_DIR GIT_CONFIG GIT_CURL_VERBOSE GIT_DIR GIT_EXEC_PATH
    GIT_EXTERNAL_DIFF GIT_INDEX_FILE GIT_NAMESPACE GIT_OBJECT_DIRECTORY
    GIT_PROXY_COMMAND GIT_SSL_CAINFO GIT_SSL_CAPATH GIT_SSL_CERT
    GIT_SSL_CERT_PASSWORD_PROTECTED GIT_SSL_KEY GIT_SSL_NO_VERIFY GIT_SSH
    GIT_SSH_COMMAND GIT_TRACE GIT_TRACE_CURL GIT_TRACE_CURL_NO_DATA
    GIT_TRACE_PACKET GIT_WORK_TREE HTTP_PROXY HTTPS_PROXY PYTHONHOME PYTHONPATH
    PYTHONNOUSERSITE PYTHONSAFEPATH PYTHONSTARTUP PYTHONUSERBASE
    SSL_CERT_DIR SSL_CERT_FILE SSH_ASKPASS ALL_PROXY NO_PROXY
    all_proxy http_proxy https_proxy no_proxy
)
for unsafe_network_var in "${unsafe_network_vars[@]}"; do
    unset "$unsafe_network_var"
done
while IFS= read -r git_config_var; do
    case "$git_config_var:${!git_config_var}" in
        GIT_CONFIG_NOSYSTEM:1|GIT_CONFIG_GLOBAL:/dev/null|GIT_CONFIG_SYSTEM:/dev/null) ;;
        *) log_error "$git_config_var is not allowed during pinned closeout"; exit 2 ;;
    esac
done < <(compgen -e GIT_CONFIG_)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ "${INTELFLO_BOOTSTRAP:-}" = "reviewed-main-v1" ]; then
    if [ -z "${INTELFLO_TRUSTED_PRIMARY:-}" ] \
        || [ -z "${INTELFLO_DELIVERY_ROOT:-}" ] \
        || [ -z "${INTELFLO_TRUSTED_REVISION:-}" ] \
        || [ -z "${INTELFLO_REMOTE_DEFAULT_REF:-}" ] \
        || [ -z "${INTELFLO_CLOSEOUT_TOOLCHAIN_OID:-}" ]; then
        log_error "Reviewed-main bootstrap metadata is incomplete"
        exit 2
    fi
    PRIMARY="$(realpath -e -- "$INTELFLO_TRUSTED_PRIMARY")"
    DELIVERY_ROOT="$(realpath -e -- "$INTELFLO_DELIVERY_ROOT")"
    if [ "$(pwd -P)" != "$DELIVERY_ROOT" ]; then
        log_error "Delivery working directory changed after trusted bootstrap"
        exit 2
    fi
    log_info "Pinned reviewed-main bootstrap revision=$INTELFLO_TRUSTED_REVISION toolchain=$INTELFLO_CLOSEOUT_TOOLCHAIN_OID"
else
    # Direct shell execution remains a test/recovery seam. The supported
    # credentialed path always supplies reviewed-main bootstrap metadata.
    PRIMARY="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
    DELIVERY_ROOT="$(git rev-parse --show-toplevel)"
fi
if [ "$DO_MERGE" -eq 1 ] && [ "${INTELFLO_BOOTSTRAP:-}" != "reviewed-main-v1" ]; then
    log_error "--merge requires the reviewed-main Python bootstrap"
    exit 2
fi
CONFIG_PATH="$PRIMARY/.git/config"
PYTHON_BIN="${INTELFLO_PYTHON_BIN:-/usr/bin/python3}"
PYTHON_HELPER_BIN="${INTELFLO_PYTHON_BIN:-$(command -v python3)}"
GIT_BIN="${INTELFLO_GIT_BIN:-$(command -v git)}"
BASH_BIN="${INTELFLO_BASH_BIN:-/bin/bash}"
if [[ ! "$PYTHON_BIN" = /* ]] || [ ! -x "$PYTHON_BIN" ]; then
    log_error "Pinned Python executable is unavailable"
    exit 2
fi
if [[ ! "$PYTHON_HELPER_BIN" = /* ]] || [ ! -x "$PYTHON_HELPER_BIN" ]; then
    log_error "Closeout helper Python executable is unavailable"
    exit 2
fi
if [[ ! "$GIT_BIN" = /* ]] || [ ! -x "$GIT_BIN" ]; then
    log_error "Pinned Git executable is unavailable"
    exit 2
fi
if [[ ! "$BASH_BIN" = /* ]] || [ ! -x "$BASH_BIN" ]; then
    log_error "Pinned Bash executable is unavailable"
    exit 2
fi
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1
python3() { PYTHONPATH="$SCRIPT_DIR" "$PYTHON_HELPER_BIN" -P "$@"; }
if [ "${INTELFLO_BOOTSTRAP:-}" = "reviewed-main-v1" ]; then
    git() { "$GIT_BIN" "$@"; }
fi

git_config_security_digest() {
    "$PYTHON_BIN" -I "$SCRIPT_DIR/git_config_security.py" "$CONFIG_PATH"
}

git_config_origin_url() {
    "$PYTHON_BIN" -I "$SCRIPT_DIR/git_config_security.py" \
        --origin-url "$CONFIG_PATH"
}

export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
GH_BIN="${INTELFLO_GH_BIN:-$(command -v gh)}"
if [[ ! "$GH_BIN" = /* ]] || [ ! -x "$GH_BIN" ]; then
    log_error "gh executable is unavailable for pinned GitHub access"
    exit 2
fi
export GIT_CONFIG_COUNT=5
export GIT_CONFIG_KEY_0="core.hooksPath"
export GIT_CONFIG_VALUE_0=/dev/null
export GIT_CONFIG_KEY_1="credential.https://github.com.helper"
export GIT_CONFIG_VALUE_1=""
export GIT_CONFIG_KEY_2="credential.https://github.com.helper"
export GIT_CONFIG_VALUE_2="!$GH_BIN auth git-credential"
export GIT_CONFIG_KEY_3="credential.https://gist.github.com.helper"
export GIT_CONFIG_VALUE_3=""
export GIT_CONFIG_KEY_4="credential.https://gist.github.com.helper"
export GIT_CONFIG_VALUE_4="!$GH_BIN auth git-credential"
export GIT_TERMINAL_PROMPT=0
export GIT_NO_REPLACE_OBJECTS=1

# Composed gate helpers construct and validate their own Git command
# environment. Do not leak this shell's credential overlay into that owner.
run_composed_git_authority() (
    local git_config_count="${GIT_CONFIG_COUNT:-0}"
    unset GIT_CONFIG_COUNT
    local index
    for ((index = 0; index < git_config_count; index++)); do
        unset "GIT_CONFIG_KEY_$index" "GIT_CONFIG_VALUE_$index"
    done
    if [ "$1" = "git" ]; then
        shift
        assert_git_config_binding || return $?
        "$GIT_BIN" \
            -c core.hooksPath=/dev/null \
            -c credential.https://github.com.helper= \
            -c "credential.https://github.com.helper=!$GH_BIN auth git-credential" \
            "$@"
        return
    fi
    "$@"
)

if [ ! -f "$CONFIG_PATH" ] || [ -L "$CONFIG_PATH" ]; then
    log_error "Canonical Git configuration is unavailable"
    exit 2
fi
if ! actual_digest="$(git_config_security_digest)"; then
    log_error "Canonical Git config uses unmeasured execution, redirect, include, URL rewrite, or worktree config"
    exit 2
fi

if [ -z "$ORIGIN_URL" ]; then
    GIT_CONFIG_SHA256="$actual_digest"
    if ! ORIGIN_URL="$(git_config_origin_url)"; then
        log_error "Canonical Git origin cannot be derived"
        exit 2
    fi
    rebound_digest="$(git_config_security_digest)"
    if [ "$rebound_digest" != "$GIT_CONFIG_SHA256" ]; then
        log_error "Git configuration changed while deriving repository binding"
        exit 2
    fi
elif [ "$actual_digest" != "$GIT_CONFIG_SHA256" ]; then
    log_error "Pinned Git configuration changed before network access"
    exit 2
fi
if [[ ! "$ORIGIN_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]]; then
    log_error "--origin-url must be one canonical GitHub HTTPS URL"
    exit 2
fi
if [[ ! "$GIT_CONFIG_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    log_error "--git-config-sha256 must be 64 lowercase hex"
    exit 2
fi
if [ -n "${GH_HOST:-}" ] && [ "$GH_HOST" != "github.com" ]; then
    log_error "GH_HOST conflicts with the canonical github.com origin"
    exit 2
fi
repository_slug="${ORIGIN_URL#https://github.com/}"
repository_slug="${repository_slug%.git}"
export GH_HOST="github.com"
export GH_REPO="github.com/$repository_slug"
ORIGIN_REMOTE="$ORIGIN_URL"
origin_args=(--origin-url "$ORIGIN_URL")
config_args=(--git-config-sha256 "$GIT_CONFIG_SHA256")

assert_git_config_binding() {
    local actual
    if [ ! -f "$CONFIG_PATH" ] || [ -L "$CONFIG_PATH" ]; then
        log_error "Pinned Git configuration is unavailable" >&2
        return 1
    fi
    actual="$(git_config_security_digest)" || {
        log_error "Pinned Git configuration cannot be read" >&2
        return 1
    }
    if [ "$actual" != "$GIT_CONFIG_SHA256" ]; then
        log_error "Pinned Git configuration changed before network access" >&2
        return 1
    fi
}

gh() {
    assert_git_config_binding || return $?
    "$GH_BIN" "$@"
}

git_primary_network() {
    assert_git_config_binding || return $?
    git -C "$PRIMARY" "$@"
}

git_network() {
    assert_git_config_binding || return $?
    git "$@"
}

# Validate the portable handoff before any merge mutation. An explicit
# --run-id is reserved for recovery of a malformed or historical PR body.
if [ -z "$RUN_ID" ]; then
    RUN_ID="$(gh pr view "$PR" --json body -q .body \
        | PYTHONPATH="$SCRIPT_DIR" python3 -c 'import sys; from skill_run_log import extract_run_id_marker; print(extract_run_id_marker(sys.stdin.read()) or "")')"
fi
if [ -z "$RUN_ID" ]; then
    log_error "PR #$PR has no valid <!-- skill-run-id: ... --> marker; pass --run-id only for explicit recovery"
    exit 1
fi
run_owner_status=0
run_owner_args=(--skill "$SKILL" --run-id "$RUN_ID" --check-owner --print-delivery-contract)
if [ "$RUN_ID_EXPLICIT" -eq 1 ]; then
    run_owner_args+=(--allow-missing-owner)
fi
run_owner_output="$(python3 "$SCRIPT_DIR/skill_run_log.py" \
    "${run_owner_args[@]}" 2>&1)" \
    || run_owner_status=$?
if [ "$run_owner_status" -ne 0 ]; then
    printf '%s\n' "$run_owner_output" >&2
    if [ "$run_owner_status" -eq 2 ]; then
        log_error "Closeout run identity must match an existing skill-owned run"
    else
        log_error "Could not validate the closeout run owner"
    fi
    exit "$run_owner_status"
fi
settlement_recovery=0
delivery_contract="$run_owner_output"
case "$delivery_contract" in
    intelflo-v1|loop-zero-v1) ;;
    *) log_error "Closeout delivery contract is invalid"; exit 1 ;;
esac
if [ "$RUN_ID_EXPLICIT" -eq 0 ]; then
    active_run_id="$(python3 "$SCRIPT_DIR/agent_dispatch.py" active-run-id \
        --worktree "$DELIVERY_ROOT")"
    if [ "$active_run_id" != "$RUN_ID" ]; then
        recovery_capsule="$PRIMARY/.audit/delivery-closeout/pr-$PR-$RUN_ID.json"
        if [ -z "$active_run_id" ] && run_composed_git_authority python3 "$SCRIPT_DIR/delivery_pipeline.py" \
            capsule-status --capsule "$recovery_capsule" --run-id "$RUN_ID" --pr "$PR" \
            >/dev/null 2>&1; then
            settlement_recovery=1
            log_warn "Resuming terminal run $RUN_ID from its closeout capsule"
        else
            log_error "PR #$PR skill-run marker does not identify the active outer run"
            exit 1
        fi
    fi
fi
# Refuse any remote or local closeout mutation until every governed unit in
# this outer run has a terminal completion and every completed dispatch has an
# independent passing verdict. The checker also fails immediately if a write
# worker still owns this worktree.
if ! lifecycle_output="$(python3 "$SCRIPT_DIR/agent_dispatch.py" lifecycle-check \
    --run-id "$RUN_ID" \
    --worktree "$DELIVERY_ROOT" \
    --stage closeout 2>&1)"; then
    printf '%s\n' "$lifecycle_output" >&2
    log_error "Dispatch lifecycle gate blocked close-out; the error above names the condition."
    log_error "Do not bypass lifecycle debt, non-current run evidence, invalid input, or an active writer lease."
    log_error "Skipping this check is safe only after proving the run has no dispatch records at any policy and no active writer; preserve the run log manually."
    exit 1
fi
printf '%s\n' "$lifecycle_output"
log_info "Dispatch lifecycle complete for $RUN_ID"

# The stable PR-marker run crosses any session handoff. Enter closeout only
# after the final-SHA verdict is proven (or intentionally skipped for docs) and
# immediately before merge. Recovery and post-merge reconciliation enter only
# after the remote MERGED state is confirmed below. The local guard avoids a
# duplicate transition within one invocation; the logger remains idempotent
# across recovery invocations.
closeout_transitioned=0
transition_closeout() {
    if [ "$settlement_recovery" -eq 0 ] \
        && [ "$delivery_contract" = "intelflo-v1" ] \
        && [ "$closeout_transitioned" -eq 0 ] \
        && { [ "$SKILL" = "work-issue" ] || [ "$SKILL" = "execute-blueprint" ]; }; then
        closeout_transitioned=1
        local phase_output
        if ! phase_output="$(python3 "$SCRIPT_DIR/skill_run_log.py" \
            --skill "$SKILL" --run-id "$RUN_ID" --transition closeout 2>&1)"; then
            log_warn "Coarse phase timing was not recorded for $RUN_ID; continuing through authoritative closeout gates."
            [ -z "$phase_output" ] || printf '%s\n' "$phase_output" >&2
            return 0
        fi
        [ -z "$phase_output" ] || printf '%s\n' "$phase_output"
    fi
}

blueprint_has_issue() {
    local commit_oid="$1"
    local blueprint_path="$2"
    local expected_issue="$3"

    git -C "$PRIMARY" show "$commit_oid:$blueprint_path" 2>/dev/null |
        awk -v expected_issue="$expected_issue" '
            NR == 1 {
                if ($0 != "---") exit 1
                in_frontmatter = 1
                next
            }
            in_frontmatter && $0 == "---" { exit }
            in_frontmatter && $0 ~ ("^issue:[[:space:]]*" expected_issue "[[:space:]]*$") {
                found = 1
                exit
            }
            END { exit !found }
        '
}

assert_blueprint_completion_before_merge() {
    [ -n "$ISSUE" ] || [ -n "$BLUEPRINT" ] || return 0

    local base_oid
    head_oid="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"
    # `baseRefOid` is not available in older gh releases. The REST payload's
    # base.sha is the exact PR base commit and is stable across supported CLIs.
    base_oid="$(gh api "repos/{owner}/{repo}/pulls/$PR" --jq .base.sha)"
    [ -n "$head_oid" ] && [ -n "$base_oid" ] || {
        log_error "Could not determine the base and head commits for PR #$PR"
        exit 1
    }

    for commit_oid in "$base_oid" "$head_oid"; do
        if ! git -C "$PRIMARY" cat-file -e "$commit_oid^{commit}" 2>/dev/null; then
            git_primary_network fetch --quiet "$ORIGIN_REMOTE" "$commit_oid"
        fi
        if ! git -C "$PRIMARY" cat-file -e "$commit_oid^{commit}" 2>/dev/null; then
            log_error "Could not read PR #$PR commit $commit_oid for blueprint verification"
            exit 1
        fi
    done

    local explicit_slug="${BLUEPRINT%.md}"
    local blueprint_path blueprint_slug completed_path active_path
    local -a candidates=()
    local -a selected=()
    local -A selected_paths=()
    for commit_oid in "$base_oid" "$head_oid"; do
        while IFS= read -r blueprint_path; do
            [ -n "$blueprint_path" ] || continue
            blueprint_slug="${blueprint_path##*/}"
            blueprint_slug="${blueprint_slug%.md}"
            if { [ -n "$ISSUE" ] && blueprint_has_issue "$commit_oid" "$blueprint_path" "$ISSUE"; } \
                || { [ -n "$BLUEPRINT" ] && [ "$blueprint_slug" = "$explicit_slug" ]; }; then
                candidates+=("$blueprint_path")
            fi
        done < <(git -C "$PRIMARY" ls-tree -r --name-only "$commit_oid" -- docs/design/blueprints)
    done

    for blueprint_path in "${candidates[@]}"; do
        blueprint_slug="${blueprint_path##*/}"
        blueprint_slug="${blueprint_slug%.md}"
        completed_path="docs/design/blueprints/completed/${blueprint_path##*/}"
        if [ -z "${selected_paths[$completed_path]:-}" ]; then
            selected_paths["$completed_path"]="$blueprint_slug"
            selected+=("$completed_path")
        fi
    done

    if [ "${#selected[@]}" -eq 0 ]; then
        if [ -n "$BLUEPRINT" ]; then
            log_error "Blueprint $BLUEPRINT is not present in PR #$PR's lifecycle tree"
            exit 1
        fi
        return 0
    fi

    for completed_path in "${selected[@]}"; do
        blueprint_slug="${selected_paths[$completed_path]}"
        if ! git -C "$PRIMARY" cat-file -e "$head_oid:$completed_path" 2>/dev/null; then
            log_error "PR #$PR head must contain $completed_path before merging issue #${ISSUE:-lifecycle delivery}."
            log_error "Recovery: make docs-blueprint-complete SLUG=$blueprint_slug"
            exit 1
        fi
        active_path="docs/design/blueprints/active/${completed_path##*/}"
        if git -C "$PRIMARY" cat-file -e "$head_oid:$active_path" 2>/dev/null; then
            log_error "PR #$PR head contains both $active_path and $completed_path."
            log_error "Recovery: make docs-blueprint-complete SLUG=$blueprint_slug"
            exit 1
        fi
    done

    log_info "PR #$PR already contains lifecycle blueprint completion"
}

# Blueprint completion belongs to the reviewed PR. Verify it before a merge can
# change remote state; closeout never moves blueprints or creates a follow-up.
assert_blueprint_completion_before_merge

# An in-progress closeout is reserved for the one call-time exception where a
# merged PR is intentionally waiting on post-merge acceptance. Keep the
# exception explicit and bounded: no free-form notes can authorize it, the live
# PR must already use Refs (not a closing keyword) for the same issue, and the
# live issue state is checked before any final-CI or merge mutation.
deferred_post_merge_acceptance=0
if [ "$OUTCOME" = "in_progress" ]; then
    if [ -z "$ISSUE" ]; then
        log_error "--outcome in_progress requires --issue"
        exit 2
    fi
    if [ -z "$NOTES" ] || [ "${#NOTES}" -gt 100 ] \
        || [[ ! "$NOTES" =~ ^post-merge-acceptance:[A-Za-z0-9][A-Za-z0-9._:/#-]{0,77}$ ]]; then
        log_error "--outcome in_progress requires --notes post-merge-acceptance:<reference> (≤100 chars)"
        exit 2
    fi
    if [ "$SKILL" != "work-issue" ] && [ "$SKILL" != "execute-blueprint" ]; then
        log_error "--outcome in_progress is reserved for work-issue or execute-blueprint"
        exit 2
    fi
    deferred_active_run_id="$(python3 "$SCRIPT_DIR/agent_dispatch.py" active-run-id \
        --worktree "$DELIVERY_ROOT")"
    if [ "$deferred_active_run_id" != "$RUN_ID" ]; then
        log_error "--outcome in_progress requires --run-id to identify the active outer run"
        exit 2
    fi
    if ! deferred_run_owner="$(PYTHONPATH="$SCRIPT_DIR" python3 - \
        "$PRIMARY/.audit/skill-runs" "$RUN_ID" <<'PY'
import sys
from pathlib import Path

from skill_run_log import load_entries

audit_dir = Path(sys.argv[1])
run_id = sys.argv[2]
rows = [row for row in load_entries(audit_dir) if row.get("run_id") == run_id]
if not rows or rows[-1].get("outcome") != "in_progress":
    raise SystemExit(1)
skill = rows[-1].get("skill")
if not isinstance(skill, str):
    raise SystemExit(1)
print(skill)
PY
    )"; then
        log_error "--outcome in_progress requires an active lifecycle row for --run-id"
        exit 2
    fi
    if [ "$deferred_run_owner" != "$SKILL" ]; then
        log_error "--outcome in_progress requires --skill to own the active logical run"
        exit 2
    fi
    # Bounded PR-body admission only: match Refs / closing keywords as text.
    # Never eval or interpolate the body into a shell command.
    if ! gh pr view "$PR" --json body -q .body | python3 -c '
import re
import sys

issue = sys.argv[1]
if not re.fullmatch(r"[1-9][0-9]*", issue):
    raise SystemExit(2)
body = sys.stdin.read()
escaped = re.escape(issue)
reference = rf"(?:(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#{escaped})(?![0-9])"
keyword_separator = r"(?:[ \t]+|:[ \t]*)"
has_refs = any(
    re.search(rf"(?i)\bRefs?{keyword_separator}{reference}", line)
    for line in body.splitlines()
)
has_closing = any(
    re.search(
        rf"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
        rf"{keyword_separator}{reference}",
        line,
    )
    for line in body.splitlines()
)
raise SystemExit(0 if has_refs and not has_closing else 1)
' "$ISSUE"; then
        log_error "--outcome in_progress requires PR body Refs #$ISSUE without Closes/Fixes/Resolves for that issue"
        exit 2
    fi
    deferred_admission_state="$(gh issue view "$ISSUE" --json state -q .state)"
    if [ "$deferred_admission_state" != "OPEN" ]; then
        log_error "--outcome in_progress requires issue #$ISSUE to be OPEN at closeout admission"
        exit 2
    fi
    deferred_post_merge_acceptance=1
fi

# 1. Merge (optional) — the project-owned gate is a compensating control for
# private repos where native GitHub required checks / merge queue are
# unavailable. It still never lets gh attempt the local checkout dance.
if [ "$DO_MERGE" -eq 1 ]; then
    state="$(gh pr view "$PR" --json state -q .state)"
    if [ "$state" = "MERGED" ]; then
        log_warn "PR #$PR already merged — skipping merge step"
    else
        expected_head="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"
        [ -n "$expected_head" ] || {
            log_error "PR #$PR has no head SHA for final-CI and merge admission"
            exit 1
        }
        base_ref="$(gh pr view "$PR" --json baseRefName -q .baseRefName)"
        [ -n "$base_ref" ] || {
            log_error "PR #$PR has no base branch for review-risk admission"
            exit 1
        }
        if ! run_composed_git_authority git check-ref-format --branch "$base_ref" >/dev/null; then
            log_error "PR #$PR has an unsafe base branch for review-risk admission"
            exit 1
        fi
        base_remote="${ORIGIN_URL:-origin}"
        run_composed_git_authority git fetch --quiet "$base_remote" \
            "+refs/heads/$base_ref:refs/remotes/origin/$base_ref"
        expected_base="$(run_composed_git_authority git merge-base "refs/remotes/origin/$base_ref" "$expected_head")"
        [[ "$expected_base" =~ ^[0-9a-f]{40}$ ]] || {
            log_error "PR #$PR has no valid merge base for review-risk admission"
            exit 1
        }
        run_composed_git_authority python3 "$SCRIPT_DIR/final_ci_gate.py" --pr "$PR" --repo "$PRIMARY" \
            --workflow-preflight-only \
            "${origin_args[@]}" "${config_args[@]}"
        review_risk_result="$(run_composed_git_authority python3 "$SCRIPT_DIR/delivery_review_risk.py" \
            --repo "$PRIMARY" \
            --verify-publication \
            --evidence-dir "$PRIMARY/.audit/pr-publications" \
            --pr "$PR" \
            --run-id "$RUN_ID" \
            --expected-head "$expected_head" \
            --expected-base "$expected_base" \
            "${origin_args[@]}" "${config_args[@]}")"
        effective_risk_tier="$(printf '%s' "$review_risk_result" | python3 -c \
            'import json, sys; print(json.load(sys.stdin)["effective_tier"])')"
        if [ "$effective_risk_tier" = "T0" ]; then
            log_info "Deterministic T0 PR #$PR uses green required checks; skipping final CI"
        else
            # final_ci_gate owns FinalCIFailureV1/FinalCIReproReceiptV1
            # validation and binds retry admission to this captured merge head.
            run_composed_git_authority python3 "$SCRIPT_DIR/final_ci_gate.py" --pr "$PR" --repo "$PRIMARY" \
                --expected-head "$expected_head" \
                "${origin_args[@]}" "${config_args[@]}"
        fi
        run_composed_git_authority python3 "$SCRIPT_DIR/pr_merge_gate.py" --pr "$PR" --repo "$PRIMARY" "${origin_args[@]}" "${config_args[@]}" \
            --wait --expected-head "$expected_head"
        if [ "$deferred_post_merge_acceptance" -eq 1 ]; then
            deferred_pre_merge_state="$(gh issue view "$ISSUE" --json state -q .state)"
            if [ "$deferred_pre_merge_state" != "OPEN" ]; then
                log_error "--outcome in_progress requires issue #$ISSUE to be OPEN immediately before merge"
                exit 2
            fi
        fi
        transition_closeout
        run_composed_git_authority python3 "$SCRIPT_DIR/pr_merge_gate.py" --pr "$PR" --repo "$PRIMARY" "${origin_args[@]}" "${config_args[@]}" \
            --merge --expected-head "$expected_head"
    fi
fi

# 2. Verify remote merge state BEFORE any cleanup (never retry a merge based
# on a failed local step — the remote may already have succeeded).
state="$(gh pr view "$PR" --json state -q .state)"
if [ "$state" != "MERGED" ]; then
    log_error "PR #$PR is $state, not MERGED. Close-out only runs after merge."
    exit 1
fi
head_ref="$(gh pr view "$PR" --json headRefName -q .headRefName)"
head_oid="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"
merge_oid="$(gh pr view "$PR" --json mergeCommit -q .mergeCommit.oid)"
[ -n "$head_ref" ] && git check-ref-format "refs/heads/$head_ref" >/dev/null 2>&1 || {
    log_error "PR #$PR has an invalid head ref"
    exit 1
}
[[ "$head_oid" =~ ^[0-9a-f]{40}$ ]] || {
    log_error "PR #$PR has an invalid head commit identity"
    exit 1
}
[[ "$merge_oid" =~ ^[0-9a-f]{40}$ ]] || {
    log_error "PR #$PR has an invalid merge commit identity"
    exit 1
}
log_info "PR #$PR merged (head: $head_ref)"
transition_closeout

# 3. Post-merge Finding Ledger reconciliation. Fetch explicitly and prove the
# merge is present on origin/default before any classifier or ledger mutation.
# A missing review leaves important+ findings candidate-resolved and emits a
# bounded continuation; it never blocks cleanup of an already-merged PR.
if [ "${INTELFLO_BOOTSTRAP:-}" = "reviewed-main-v1" ]; then
    remote_default_ref="$INTELFLO_REMOTE_DEFAULT_REF"
else
    default_branch="$(git -C "$PRIMARY" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
    default_branch="${default_branch:-main}"
    remote_default_ref="refs/heads/$default_branch"
fi
case "$remote_default_ref" in
    refs/heads/*) ;;
    *) log_error "Remote default branch identity is invalid"; exit 1 ;;
esac
git check-ref-format "$remote_default_ref" >/dev/null 2>&1 || {
    log_error "Remote default branch identity is invalid"
    exit 1
}
default_branch="${remote_default_ref#refs/heads/}"
# Re-park the canonical primary before the fallible settlement tail. Once the
# remote merge is visible, leaving this until final cleanup can strand recovery:
# the next trusted bootstrap correctly rejects a primary parked before the
# merge. Hold the writer lease across fetch, cleanliness recheck, containment,
# and checkout, and make failure terminal for this invocation.
remote_default_after_merge=""
if ! remote_default_after_merge="$(
    # worktree_guard owns only the lease here; the nested fetch remains this
    # launcher's responsibility and therefore retains the pinned credential
    # overlay constructed above.
    python3 "$SCRIPT_DIR/worktree_guard.py" exec \
        --worktree "$PRIMARY" \
        --boundary "pr-closeout-primary-repark" \
        --timeout 300 \
        -- "$BASH_BIN" -c '
            set -euo pipefail
            primary="$1"
            origin_url="$2"
            git_config_sha256="$3"
            python_bin="$4"
            git_config_security="$5"
            remote_default_ref="$6"
            merge_oid="$7"
            if [ -n "$(git -C "$primary" status --porcelain --untracked-files=all)" ]; then
                echo "Primary re-park failed — canonical primary is dirty" >&2
                exit 1
            fi
            if [ -n "$git_config_sha256" ]; then
                config_path="$primary/.git/config"
                if [ ! -f "$config_path" ] || [ -L "$config_path" ]; then
                    echo "Primary re-park failed — pinned Git configuration is unavailable" >&2
                    exit 1
                fi
                actual="$("$python_bin" -I "$git_config_security" "$config_path")" || exit 1
                if [ "$actual" != "$git_config_sha256" ]; then
                    echo "Primary re-park failed — pinned Git configuration changed" >&2
                    exit 1
                fi
            fi
            fetch_remote="${origin_url:-origin}"
            git -C "$primary" fetch "$fetch_remote" -q "$remote_default_ref"
            if [ -n "$(git -C "$primary" status --porcelain --untracked-files=all)" ]; then
                echo "Primary re-park failed — canonical primary became dirty during refresh" >&2
                exit 1
            fi
            fresh_oid="$(git -C "$primary" rev-parse --verify FETCH_HEAD)"
            if [[ ! "$fresh_oid" =~ ^[0-9a-f]{40}$ ]]; then
                echo "Primary re-park failed — fetched default branch identity is invalid" >&2
                exit 1
            fi
            if ! git -C "$primary" merge-base --is-ancestor "$merge_oid" "$fresh_oid"; then
                echo "Primary re-park failed — merge is absent from the fetched default branch" >&2
                exit 1
            fi
            git -C "$primary" checkout -q --detach "$fresh_oid"
            printf "%s\n" "$fresh_oid"
        ' "$BASH_BIN" "$PRIMARY" "$ORIGIN_URL" "$GIT_CONFIG_SHA256" "$PYTHON_BIN" \
        "$SCRIPT_DIR/git_config_security.py" "$remote_default_ref" "$merge_oid"
)"; then
    log_error "Primary re-park failed before post-merge settlement; repair the primary and retry closeout"
    exit 1
fi
log_info "Primary re-parked on fresh origin/$default_branch"
containment_ready=1

reconciliation_dir="$PRIMARY/.audit/finding-reconciliation"
reconciliation_prefix="pr-$PR-${merge_oid:0:12}"
mkdir -p "$reconciliation_dir"

write_reconciliation_continuation() {
    local reason="$1"
    if ! RECONCILE_REASON="$reason" RECONCILE_IDS="${finding_ids[*]:-}" \
        RECONCILE_PR="$PR" RECONCILE_MERGE="$merge_oid" \
        RECONCILE_RUN="$RUN_ID" python3 - "$reconciliation_dir/$reconciliation_prefix-continuation.json" <<'PY'
import json
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "finding_ids": os.environ.get("RECONCILE_IDS", "").split(),
            "merge_commit": os.environ["RECONCILE_MERGE"],
            "mode": "post-merge-reconciliation-continuation",
            "pr": int(os.environ["RECONCILE_PR"]),
            "reason": os.environ["RECONCILE_REASON"],
            "run_id": os.environ["RECONCILE_RUN"],
            "terminal": False,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
    then
        log_warn "Finding reconciliation pending but its continuation file could not be written: $reason"
        return 0
    fi
    log_warn "Finding reconciliation pending: $reason"
    log_warn "Continuation: $reconciliation_dir/$reconciliation_prefix-continuation.json"
}

clear_orphan_trailer_reconciliation_artifacts() {
    local continuation_path="$reconciliation_dir/$reconciliation_prefix-continuation.json"
    local plan_path="$reconciliation_dir/$reconciliation_prefix-plan.json"
    if [ -f "$continuation_path" ] && ! RECONCILE_PR="$PR" \
        RECONCILE_MERGE="$merge_oid" RECONCILE_RUN="$RUN_ID" \
        python3 - "$continuation_path" <<'PY'
import json
import os
import sys
from pathlib import Path

allowed_reasons = {
    "canonical Finding Ledger could not be read for exact-ID reconciliation",
    "read-only reconciliation plan rejected merge provenance",
}
path = Path(sys.argv[1])
record = json.loads(path.read_text(encoding="utf-8"))
valid = (
    isinstance(record, dict)
    and record.get("mode") == "post-merge-reconciliation-continuation"
    and record.get("pr") == int(os.environ["RECONCILE_PR"])
    and record.get("merge_commit") == os.environ["RECONCILE_MERGE"]
    and record.get("run_id") == os.environ["RECONCILE_RUN"]
    and record.get("reason") in allowed_reasons
)
if not valid:
    raise SystemExit("continuation does not belong to orphan-trailer reconciliation")
PY
    then
        log_warn "Preserved an unrelated or invalid reconciliation continuation"
        return 1
    fi
    rm -f "$continuation_path" "$plan_path"
}

finding_ids=()
if [ "$containment_ready" -eq 0 ]; then
    write_reconciliation_continuation \
        "origin/$default_branch fetch failed; primary containment is unproven"
elif ! git -C "$PRIMARY" merge-base --is-ancestor "$merge_oid" "$remote_default_after_merge"; then
    write_reconciliation_continuation \
        "merge $merge_oid is not contained by origin/$default_branch after fetch"
elif [ "$delivery_contract" = "loop-zero-v1" ]; then
    log_info "PR-scoped findings expired at verified merge; raw review history retained"
elif ! merge_ids_output="$(
    git -C "$PRIMARY" show -s --format=%B "$merge_oid" \
        | run_composed_git_authority python3 "$SCRIPT_DIR/pr_merge_gate.py" --repo "$PRIMARY" --extract-finding-ids
)"; then
    write_reconciliation_continuation "merge commit contains a malformed Fixes trailer"
else
    if [ -n "$merge_ids_output" ]; then
        mapfile -t finding_ids <<<"$merge_ids_output"
    fi
    expected_ids=()
    if ! expected_ids_output="$(
        git -C "$PRIMARY" log --format='%B%x00' "$merge_oid^..$head_oid" \
            | tr '\0' '\n' \
            | run_composed_git_authority python3 "$SCRIPT_DIR/pr_merge_gate.py" --repo "$PRIMARY" --extract-finding-ids
    )"; then
        write_reconciliation_continuation "PR commits contain a malformed Fixes trailer"
    else
        if [ -n "$expected_ids_output" ]; then
            mapfile -t expected_ids <<<"$expected_ids_output"
        fi
        missing_ids=()
        for finding_id in "${expected_ids[@]}"; do
            if ! printf '%s\n' "${finding_ids[@]}" | grep -Fqx "$finding_id"; then
                missing_ids+=("$finding_id")
            fi
        done
        if [ "${#missing_ids[@]}" -gt 0 ]; then
            write_reconciliation_continuation \
                "merge commit omitted expected trailers: ${missing_ids[*]}"
        elif [ "${#finding_ids[@]}" -gt 0 ]; then
            filter_args=()
            for finding_id in "${finding_ids[@]}"; do
                filter_args+=(--finding-id "$finding_id")
            done
            if ! deposited_ids_output="$(
                python3 "$SCRIPT_DIR/finding_anchor.py" \
                    filter-known \
                    --worktree "$PRIMARY" \
                    "${filter_args[@]}"
            )"; then
                write_reconciliation_continuation \
                    "canonical Finding Ledger could not be read for exact-ID reconciliation"
            elif [ -z "$deposited_ids_output" ]; then
                finding_ids=()
                # The immutable merge trailers are the durable record of the
                # skipped IDs. Do not create a second waiver or reconciliation
                # artifact for IDs that have no ledger state to mutate. Only
                # clear a replay artifact proven to belong to this exact run;
                # an unrelated blocker remains authoritative.
                clear_orphan_trailer_reconciliation_artifacts || true
                log_info "No deposited exact Finding Ledger trailers require reconciliation"
            else
            mapfile -t finding_ids <<<"$deposited_ids_output"
            finding_args=()
            for finding_id in "${finding_ids[@]}"; do
                finding_args+=(--finding-id "$finding_id")
            done
            common_reconcile_args=(
                classify
                --worktree "$PRIMARY"
                --target "$merge_oid"
                --json
                --provenance-pr "$PR"
                --provenance-commit "$merge_oid"
            )
            review_task_id=""
            publication_lookup_ready=1
            if ! review_task_id="$(RECONCILE_PR="$PR" RECONCILE_REVIEWED_HEAD="$head_oid" python3 - "$PRIMARY/.audit/pr-publications" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

legacy_task_ids = set()
exact_head_task_ids = set()
exact_head_exemption = False
reviewed_head = os.environ["RECONCILE_REVIEWED_HEAD"]
if re.fullmatch(r"[0-9a-f]{40}", reviewed_head) is None:
    raise SystemExit("reviewed publication head is invalid")
root = Path(sys.argv[1])
for path in sorted(root.glob("*.jsonl")) if root.is_dir() else ():
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid publication JSON: {exc}")
        if not isinstance(record, dict):
            raise SystemExit(f"{path}:{line_number}: publication record is not an object")
        if (
            record.get("pr") == int(os.environ["RECONCILE_PR"])
            and record.get("status") == "published"
        ):
            task_id = record.get("review_task_id")
            review_exemption = record.get("review_exemption")
            if task_id is None and review_exemption == "T0":
                task_id = ""
            elif not isinstance(task_id, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id
            ) is None:
                raise SystemExit(
                    f"{path}:{line_number}: published receipt has invalid review_task_id"
                )
            elif review_exemption is not None:
                raise SystemExit(
                    f"{path}:{line_number}: reviewed publication has an invalid exemption"
                )
            if "expected_head" not in record:
                if not task_id:
                    raise SystemExit(
                        f"{path}:{line_number}: exempt publication has no expected_head"
                    )
                legacy_task_ids.add(task_id)
                continue
            expected_head = record["expected_head"]
            if not isinstance(expected_head, str) or re.fullmatch(
                r"[0-9a-f]{40}", expected_head
            ) is None:
                raise SystemExit(
                    f"{path}:{line_number}: published receipt has invalid expected_head"
                )
            if expected_head == reviewed_head:
                if task_id:
                    exact_head_task_ids.add(task_id)
                else:
                    exact_head_exemption = True
if exact_head_task_ids:
    selected_task_ids = exact_head_task_ids
elif exact_head_exemption:
    selected_task_ids = set()
else:
    selected_task_ids = legacy_task_ids
if len(selected_task_ids) > 1:
    raise SystemExit("published receipts disagree on review_task_id")
print(next(iter(selected_task_ids), ""))
PY
)"; then
                publication_lookup_ready=0
                write_reconciliation_continuation \
                    "accepted-review publication receipts could not be read"
            fi
            confirmed_args=()
            plan_path="$reconciliation_dir/$reconciliation_prefix-plan.json"
            if [ "$publication_lookup_ready" -eq 0 ]; then
                finding_args=()
            elif [ -n "$review_task_id" ] && python3 "$SCRIPT_DIR/finding_anchor.py" \
                "${common_reconcile_args[@]}" \
                "${finding_args[@]}" \
                --confirmed-by-review-task "$review_task_id" >"$plan_path" 2>/dev/null; then
                confirmed_args=(--confirmed-by-review-task "$review_task_id")
            elif ! python3 "$SCRIPT_DIR/finding_anchor.py" \
                "${common_reconcile_args[@]}" \
                "${finding_args[@]}" >"$plan_path"; then
                write_reconciliation_continuation \
                    "read-only reconciliation plan rejected merge provenance"
                finding_args=()
            fi

            if [ "${#finding_args[@]}" -gt 0 ]; then
                if ! lease_groups_output="$(CLOSEOUT_RUN_ID="$RUN_ID" python3 - "$plan_path" "${finding_ids[@]}" 2>&1 <<'PY'
import json
import os
import sys
from pathlib import Path

def reject(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_ids = set(sys.argv[2:])
identity_fields = ("coordinator_id", "run_id", "worker_session_id")
groups = {}
planned_ids = set()
for item in plan.get("findings", []):
    finding_id = item.get("finding_id")
    if not isinstance(finding_id, str) or not finding_id.strip():
        reject("read-only reconciliation plan has invalid finding identity")
    if finding_id not in expected_ids:
        continue
    if finding_id in planned_ids:
        reject("read-only reconciliation plan duplicated an exact finding")
    planned_ids.add(finding_id)
    lease = item.get("lease")
    if lease is None:
        key = "unleased"
        identity = ("-", "-", "-")
    else:
        if (
            not isinstance(lease, dict)
            or not isinstance(lease.get("lease_id"), str)
            or not lease["lease_id"].strip()
            or lease["lease_id"] == "unleased"
        ):
            reject("read-only reconciliation plan has invalid lease evidence")
        key = lease["lease_id"]
        owner = lease.get("owner")
        if not isinstance(owner, dict):
            reject("lease owner identity is absent or partial")
        identity = tuple(owner.get(field) for field in identity_fields)
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            reject("lease owner identity is absent or partial")
        if identity[1] != os.environ["CLOSEOUT_RUN_ID"]:
            reject("lease owner run_id does not match closeout run")
    values = (key, *identity, finding_id)
    if any("|" in value or "\n" in value or "\r" in value for value in values):
        reject("read-only reconciliation plan contains unsafe identity evidence")
    existing = groups.setdefault(key, {"identity": identity, "finding_ids": []})
    if existing["identity"] != identity:
        reject("one active lease has multiple owner identities")
    existing["finding_ids"].append(finding_id)

if planned_ids != expected_ids:
    reject("read-only reconciliation plan omitted one or more exact findings")
if not groups:
    reject("read-only reconciliation plan contains no exact findings")
for lease_id in sorted(groups):
    group = groups[lease_id]
    print("|".join((lease_id, *group["identity"], *sorted(group["finding_ids"]))))
PY
)"; then
                    write_reconciliation_continuation "$lease_groups_output"
                    finding_args=()
                elif [ -z "$lease_groups_output" ]; then
                    write_reconciliation_continuation \
                        "read-only reconciliation plan contains no lease groups"
                    finding_args=()
                fi
            fi

            # The classifier serializes on the canonical ledger lock and
            # revalidates CAS/lease preconditions there. A concurrent change
            # therefore fails closed into one run-level continuation. Each
            # same-run lease is applied independently so a replay resumes from
            # the first incomplete idempotent operation.
            if [ "${#finding_args[@]}" -eq 0 ]; then
                : # Planning failure already emitted a durable continuation.
            else
                mapfile -t lease_groups <<<"$lease_groups_output"
                reconciliation_failed=0
                pending_total=0
                group_number=0
                for lease_group in "${lease_groups[@]}"; do
                    IFS='|' read -r -a group_values <<<"$lease_group"
                    if [ "${#group_values[@]}" -lt 5 ]; then
                        write_reconciliation_continuation \
                            "read-only reconciliation plan has a malformed lease group"
                        reconciliation_failed=1
                        break
                    fi
                    group_lease_id="${group_values[0]}"
                    group_lease_owner_args=()
                    if [ "$group_lease_id" != "unleased" ]; then
                        group_lease_owner_args=(--coordinator-id "${group_values[1]}" \
                            "--run-id" "${group_values[2]}" \
                            "--worker-session-id" "${group_values[3]}")
                    fi
                    group_finding_args=()
                    for group_finding_id in "${group_values[@]:4}"; do
                        group_finding_args+=(--finding-id "$group_finding_id")
                    done
                    group_number=$((group_number + 1))
                    applied_path="$reconciliation_dir/$reconciliation_prefix-applied-$group_number.json"
                    if ! python3 "$SCRIPT_DIR/finding_anchor.py" \
                        "${common_reconcile_args[@]}" \
                        "${group_finding_args[@]}" \
                        --apply \
                        --operation-id-prefix "pr-closeout:$PR:$merge_oid:lease:$group_lease_id" \
                        "${group_lease_owner_args[@]}" \
                        "${confirmed_args[@]}" \
                        >"$applied_path"; then
                        write_reconciliation_continuation \
                            "CAS, lease, or accepted-review precondition rejected apply for $group_lease_id"
                        reconciliation_failed=1
                        break
                    fi
                    if ! pending_count="$(PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - "$applied_path" <<'PY'
import json
import sys
from pathlib import Path
from finding_ledger import closure_authority_severity

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    sum(
        1
        for finding in plan.get("findings", [])
        if closure_authority_severity(finding) in {"critical", "important"}
        and finding.get("proposed", {}).get("state") == "open"
    )
)
PY
)"; then
                        write_reconciliation_continuation \
                            "applied reconciliation receipt was not valid JSON"
                        reconciliation_failed=1
                        break
                    fi
                    pending_total=$((pending_total + pending_count))
                done
                if [ "$reconciliation_failed" -eq 0 ] && [ "$pending_total" -gt 0 ]; then
                    write_reconciliation_continuation \
                        "$pending_total important/critical finding(s) require one bounded exact-tree review"
                elif [ "$reconciliation_failed" -eq 0 ]; then
                    rm -f "$reconciliation_dir/$reconciliation_prefix-continuation.json"
                    log_info "Reconciled ${#finding_ids[@]} exact Finding Ledger trailer(s)"
                fi
            fi
            fi
        else
            rm -f "$reconciliation_dir/$reconciliation_prefix-continuation.json"
            log_info "No exact Finding Ledger trailers require reconciliation"
        fi
    fi
fi

# 4. Settle run-owned obligations through one replayable capsule. The merge is
# already proven remote above; an interruption leaves the same ordered
# `remaining` operation IDs for the next invocation. Campaign drain is owned by
# its campaign coordinator and is intentionally absent from this plan.
if [ "$deferred_post_merge_acceptance" -eq 0 ]; then
    settlement_dir="$PRIMARY/.audit/delivery-closeout"
    settlement_capsule="$settlement_dir/pr-$PR-$RUN_ID.json"
    mkdir -p "$settlement_dir"
    settlement_args=(
        settle
        --worktree "$DELIVERY_ROOT"
        --run-id "$RUN_ID"
        --pr "$PR"
        --skill "$SKILL"
        --reviewed-head "$head_oid"
        --merge-commit "$merge_oid"
        --branch "$head_ref"
        --capsule "$settlement_capsule"
        --reconciliation-continuation "$reconciliation_dir/$reconciliation_prefix-continuation.json"
    )
    [ -n "$ISSUE" ] && settlement_args+=(--issue "$ISSUE")
    [ -n "$REVIEW_PASSES" ] && settlement_args+=(--review-passes "$REVIEW_PASSES")
    [ -n "$NOTES" ] && settlement_args+=(--notes "$NOTES")
    [ -n "$MERGE_DELTA_REVIEW_TASK" ] && settlement_args+=(--merge-delta-review-task "$MERGE_DELTA_REVIEW_TASK")
    [ -n "$MERGE_DELTA_REVIEW_RUN_ID" ] && settlement_args+=(--merge-delta-review-run-id "$MERGE_DELTA_REVIEW_RUN_ID")
    if ! job_binding_output="$("$BASH_BIN" "$SCRIPT_DIR/job.sh" binding-files --run-id "$RUN_ID")"; then
        log_error "Could not enumerate wrapper jobs owned by $RUN_ID"
        exit 1
    fi
    while IFS= read -r job_binding; do
        [ -n "$job_binding" ] && settlement_args+=(--job-binding "$job_binding")
    done <<<"$job_binding_output"
    if ! run_composed_git_authority python3 "$SCRIPT_DIR/delivery_pipeline.py" "${settlement_args[@]}"; then
        log_error "Post-merge settlement remains incomplete; retry this closeout against the same capsule."
        log_error "Recovery capsule: $settlement_capsule"
        exit 1
    fi
    log_info "Run-owned obligations settled through $settlement_capsule"
fi

# 5. Remote branch cleanup. The lease makes deletion conditional on the ref
# still naming the exact PR head that was reviewed and merged.
remote_ref="refs/heads/$head_ref"
remote_row="$(git_network ls-remote --heads "$ORIGIN_REMOTE" "$remote_ref")"
if [ -n "$remote_row" ]; then
    if [[ "$remote_row" == *$'\n'* ]]; then
        log_error "Remote branch $head_ref resolved to multiple refs; refusing cleanup"
        exit 1
    fi
    read -r remote_oid remote_name remote_extra <<<"$remote_row"
    if [[ ! "$remote_oid" =~ ^[0-9a-f]{40}$ ]] \
        || [ "$remote_name" != "$remote_ref" ] \
        || [ -n "${remote_extra:-}" ]; then
        log_error "Remote branch $head_ref returned an invalid ref identity"
        exit 1
    fi
    if [ "$remote_oid" != "$head_oid" ]; then
        log_error "Remote branch $head_ref moved after review; refusing cleanup"
        exit 1
    fi
    git_network push \
        --force-with-lease="$remote_ref:$head_oid" \
        "$ORIGIN_REMOTE" ":$remote_ref"
    log_info "Deleted remote branch $head_ref at reviewed head $head_oid"
else
    log_info "Remote branch $head_ref already gone"
fi
if [ -n "$ORIGIN_URL" ]; then
    git_primary_network fetch --prune --quiet "$ORIGIN_REMOTE" \
        '+refs/heads/*:refs/remotes/origin/*'
else
    git_primary_network fetch --prune --quiet origin
fi

# 6. Local branch cleanup is intentionally observational only. Native Git has
# no atomic "delete this ref only if no worktree owns it" primitive: another
# process can attach the branch after an ownership check but before update-ref.
# The task worktree owner removes its own branch; closeout never races it.
if git -C "$PRIMARY" show-ref --verify --quiet "refs/heads/$head_ref"; then
    wt_path="$(git -C "$PRIMARY" worktree list --porcelain | awk -v ref="refs/heads/$head_ref" '$1 == "worktree" { wt = substr($0, 10) } $1 == "branch" && $2 == ref { print wt }')"
    if [ -n "$wt_path" ]; then
        log_warn "Branch $head_ref is checked out at $wt_path — remove via ExitWorktree(action: \"remove\")"
    else
        log_warn "Local branch $head_ref retained — remove through its owning worktree lifecycle"
    fi
fi

# 7. Managed SystemError delivery lock — only after remote merge verification
# and immediately before issue-state reporting. The helper is a no-op for
# non-work-issue skills and for issues that are not managed SystemErrors with
# an active in-progress lock.
if [ -n "$ISSUE" ] && [ "$deferred_post_merge_acceptance" -eq 0 ]; then
    python3 "$SCRIPT_DIR/system_error_issue_lock.py" \
        --issue "$ISSUE" --pr "$PR" --skill "$SKILL"
fi

# 8. Issue state.
if [ -n "$ISSUE" ]; then
    issue_state="$(gh issue view "$ISSUE" --json state -q .state)"
    if [ "$deferred_post_merge_acceptance" -eq 1 ]; then
        log_info "Issue #$ISSUE retained for deferred post-merge acceptance"
    elif [ "$issue_state" = "CLOSED" ]; then
        log_info "Issue #$ISSUE closed"
    else
        log_warn "Issue #$ISSUE still OPEN — auto-close via 'Closes #$ISSUE' did not fire; close manually if the work is done: gh issue close $ISSUE --comment \"Shipped via #$PR\""
    fi
fi

# 9. Run log. Terminal merged runs are the final settlement operation above;
# that owner invokes skill_run_log.py with --verified-merged after every prior
# run-owned operation has a receipt.
# deferred post-merge acceptance deliberately retains its in-progress row.
if [ "$deferred_post_merge_acceptance" -eq 1 ]; then
    log_info "Existing in-progress run log retained for deferred post-merge acceptance"
else
    log_info "Run log terminalized as the final settlement operation"
fi

# 10. Re-entry capsule — hard, idempotent logical-task handoff for delivery
# orchestrators. Other historical callers keep their existing closeout path.
if [ "$deferred_post_merge_acceptance" -eq 0 ] \
    && [ "$delivery_contract" = "intelflo-v1" ] \
    && { [ "$SKILL" = "work-issue" ] || [ "$SKILL" = "execute-blueprint" ]; }; then
  capsule_args=(
    write
    --run-id "$RUN_ID"
    --outer-skill "$SKILL"
    --outcome "$OUTCOME"
    --pr "$PR"
    --branch "$head_ref"
    --worktree "$DELIVERY_ROOT"
    --head-sha "$head_oid"
    --last-verified-sha "$head_oid"
    --completed-check pr-merge-gate
    --next-action refresh_backlog
    --boundary-disposition rollover
  )
  [ -n "$ISSUE" ] && capsule_args+=(--issue "$ISSUE")
  python3 "$SCRIPT_DIR/reentry_capsule.py" "${capsule_args[@]}"
  log_info "Re-entry capsule emitted to the primary checkout"
fi

echo ""
log_info "Close-out done. Remaining manual steps (if applicable):"
echo "  • Resolve open review threads (gh api graphql resolveReviewThread)"
echo "  • Drop commit-autofix-temp-* stash if one exists"
echo "  • ExitWorktree(action: \"remove\") — discard_changes: true is expected after a squash merge"
