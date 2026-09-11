#!/usr/bin/env bash
# Claude Code PreToolUse hook (matcher: Bash). Warns on known-destructive
# command shapes from incident history. WARN-ONLY for the first release
# (blueprint 2026-07-10-meta-system-metabolism Phase 1); may become blocking
# after a clean observation window.
#
# Shapes guarded:
#   - `railway link`               — stateful link races across parallel worktrees
#   - bare `git stash pop|apply|drop` — cross-session stash loss unless it is the
#     commit-autofix temp stash being handled deliberately
#   - `git checkout <branch>` / `git switch <branch>` in the PRIMARY checkout —
#     HEAD-hijack risk for sibling agents (incident #1026); worktrees are fine
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -z "$cmd" ] && exit 0

warn() { printf 'bash-guard warning: %s\n' "$1"; }

case "$cmd" in
  *"railway link"*)
    warn "'railway link' writes shared link-state that races across parallel worktrees — scope per command with -s/-e instead (observability rule)."
    ;;
esac

if printf '%s' "$cmd" | grep -qE '(^|[;&|[:space:]])git stash (pop|apply|drop)\b' \
  && ! printf '%s' "$cmd" | grep -q 'commit-autofix-temp'; then
  warn "bare 'git stash pop/apply/drop' can destroy another session's stash — verify the stash name first (parallel-agents rule)."
fi

if printf '%s' "$cmd" | grep -qE '(^|[;&|[:space:]])git (checkout|switch) (-[bBc] |[^-])' ; then
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$top" ] && [[ "$top" != *"/.claude/worktrees/"* ]] && [[ "$top" != *"/.cursor/worktrees/"* ]]; then
    warn "branch switch in the PRIMARY checkout — park primary on detached origin/main; task work belongs in a worktree (incident #1026)."
  fi
fi

exit 0
