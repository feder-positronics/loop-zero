#!/bin/bash
# Reject commits that add or modify skill content under non-canonical dirs.
# Canonical location for skills is .cursor/skills/ only.
# .agents/skills/, .agent/skills/, .claude/skills/ must only contain symlinks (no content edits there).
set -e

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# Staged paths that are *inside* a skill under non-canonical dirs (content edits)
# Match: .agents/skills/<name>/<anything>, .agent/skills/<name>/<anything>, .claude/skills/<name>/<anything>
VIOLATIONS=()
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if [[ "$path" =~ ^\.agents/skills/[^/]+/ ]] \
     || [[ "$path" =~ ^\.agent/skills/[^/]+/ ]] \
     || [[ "$path" =~ ^\.claude/skills/[^/]+/ ]]; then
    VIOLATIONS+=("$path")
  fi
done < <(git diff --cached --name-only)

if [[ ${#VIOLATIONS[@]} -gt 0 ]]; then
  echo "Skills must be edited only under .cursor/skills/ (canonical)."
  echo "The following staged paths are under a non-canonical skill dir:"
  printf '  %s\n' "${VIOLATIONS[@]}"
  echo ""
  echo "Edit under .cursor/skills/<name>/ then run: make sync-agent"
  exit 1
fi
exit 0
