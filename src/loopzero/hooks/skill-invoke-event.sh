#!/usr/bin/env bash
# Claude Code PostToolUse hook (matcher: Skill). Emits the entry-telemetry
# invoke event that SKILL.md files used to mandate in prose — deterministic,
# decay-immune, zero per-skill lines.
#
# Reads the hook payload JSON on stdin; never fails the tool call.
# Coverage note: fires in Claude Code only. Cursor/Codex skill invocations are
# not counted by this hook — `make invoke-counts` documents the coverage split.
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
skill="$(printf '%s' "$payload" | jq -r '.tool_input.skill // empty' 2>/dev/null || true)"
if [ -n "$skill" ]; then
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  python3 "$root/scripts/util/agent_event.py" invoke --skill "$skill" >/dev/null 2>&1 || true
fi
exit 0
