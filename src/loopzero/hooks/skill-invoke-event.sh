#!/usr/bin/env bash
# Claude Code hook: emits the skill-entry telemetry event that SKILL.md files
# used to mandate in prose — deterministic, decay-immune, zero per-skill lines.
#
# Wired to BOTH invocation paths (see .claude/settings.json):
#   PostToolUse         matcher Skill  → skill run as a tool  (.tool_input.skill)
#   UserPromptExpansion matcher *      → skill run as /command (command-name field)
#
# Why both: on 2026-07-25 only 8 non-`commit-autofix` invocations were recorded
# from Claude in 30 days, while a single session invoked six distinct skills. The
# PostToolUse/Skill matcher never fires for `/name` invocations, so
# `make invoke-counts` reported an artifact — `commit-autofix` looked like 92% of
# all skill use purely because `commit-auto-fix.sh` self-reports from shell while
# nothing else had a working emission path. A skill-retirement decision was
# nearly taken on that data.
#
# Never fails the caller. But it must not fail *silently*: an emitter that quietly
# drops events produces confident, wrong usage data, which is worse than none.
# When the payload carries no recognisable skill name we record an
# `invoke-unmapped` event naming the payload's keys, so the gap shows up in
# telemetry instead of looking like disuse.
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0

root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
emit="$root/scripts/util/agent_event.py"
[ -f "$emit" ] || exit 0

# Field paths differ per hook event, and the expansion payload's exact key is not
# contractually documented, so try the known shapes rather than assuming one.
skill="$(
  printf '%s' "$payload" | jq -r '
    .tool_input.skill
    // .command_name // .command
    // .skill_name // .skill
    // .expansion.command_name
    // empty
  ' 2>/dev/null || true
)"

# `/name` forms and namespaced plugin skills both occur; normalise to the bare
# name so both paths group together in the counts.
skill="${skill#/}"
skill="${skill##*:}"
skill="$(printf '%s' "$skill" | tr -d '[:space:]')"

if [ -n "$skill" ]; then
  python3 "$emit" invoke --skill "$skill" >/dev/null 2>&1 || true
  exit 0
fi

# Unrecognised payload: record the shape rather than nothing.
event="$(printf '%s' "$payload" | jq -r '.hook_event_name // "unknown"' 2>/dev/null || echo unknown)"
python3 "$emit" invoke --skill "invoke-unmapped-${event}" >/dev/null 2>&1 || true
exit 0
