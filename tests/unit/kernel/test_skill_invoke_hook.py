"""Tests for the skill-invocation telemetry hook.

The failure this guards against is not a crash — the hook is deliberately
never-fail — but a *silent* one. On 2026-07-25 only 8 non-`commit-autofix`
invocations were recorded from Claude in 30 days because the hook was wired to
the `Skill` tool path alone and never fired for `/command` invocations. The
resulting counts looked precise and were an artifact: `commit-autofix` appeared
to be 92% of all skill use purely because it self-reports from shell.

So these tests assert both that recognised payloads emit, and that an
unrecognised payload still emits *something* — silence is the bug.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOK = REPO / "src" / "loopzero" / "hooks" / "skill-invoke-event.sh"


def _sandbox(tmp_path: Path, name: str = "s") -> Path:
    """A throwaway git repo so emitted events never touch real telemetry.

    `agent_event.py` anchors `.audit/` via `git rev-parse --git-common-dir` and
    honours no env override, so isolation has to come from the working tree the
    hook runs in — not from a variable.
    """
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    target = root / "scripts" / "util"
    target.mkdir(parents=True, exist_ok=True)
    (target / "agent_event.py").write_bytes(
        (REPO / "src" / "loopzero" / "kernel" / "events.py").read_bytes()
    )
    return root


def _run(payload: dict, tmp_path: Path, name: str = "s") -> list[dict]:
    """Run the hook in a sandbox repo and return the invoke events it emitted."""
    sandbox = _sandbox(tmp_path, name)
    subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=sandbox,
        check=False,
    )
    events: list[dict] = []
    for path in sorted((sandbox / ".audit" / "agent-events").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return [e for e in events if e.get("kind") == "invoke"]


def test_hook_is_executable_and_never_fails(tmp_path: Path) -> None:
    """Run in a sandbox repo, never `cwd=REPO`.

    Running the hook in the real checkout writes real events: the unmapped
    payloads below each emit an `invoke-unmapped-unknown` marker into the
    primary `.audit/agent-events/`, polluting the telemetry this hook exists
    to keep truthful (diagnosed in #2995).
    """
    assert HOOK.exists()
    sandbox = _sandbox(tmp_path)
    for payload in ("", "not json", "{}", '{"a":1}'):
        proc = subprocess.run(
            ["bash", str(HOOK)],
            input=payload,
            text=True,
            capture_output=True,
            cwd=sandbox,
        )
        assert proc.returncode == 0, f"hook must never fail the caller: {payload!r}"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"hook_event_name": "PostToolUse", "tool_input": {"skill": "code-review"}},
            "code-review",
        ),
        (
            {
                "hook_event_name": "UserPromptExpansion",
                "command_name": "resolve-findings",
            },
            "resolve-findings",
        ),
        ({"hook_event_name": "UserPromptExpansion", "command": "/explain"}, "explain"),
        (
            {"hook_event_name": "UserPromptExpansion", "skill_name": "work-issue"},
            "work-issue",
        ),
    ],
)
def test_recognised_payloads_emit_the_bare_skill_name(
    payload: dict, expected: str, tmp_path: Path
) -> None:
    """Both invocation paths must land in the same bucket, or counts split."""
    events = _run(payload, tmp_path)
    assert [e["skill"] for e in events] == [expected]


def test_namespaced_plugin_skill_is_normalised(tmp_path: Path) -> None:
    events = _run(
        {"hook_event_name": "UserPromptExpansion", "command_name": "plugin:work-issue"},
        tmp_path,
    )
    assert [e["skill"] for e in events] == ["work-issue"]


def test_leading_slash_is_stripped(tmp_path: Path) -> None:
    """`/explain` and the Skill tool's `explain` must not count as two skills."""
    slashed = _run(
        {"hook_event_name": "UserPromptExpansion", "command": "/explain"}, tmp_path, "a"
    )
    bare = _run(
        {"hook_event_name": "PostToolUse", "tool_input": {"skill": "explain"}},
        tmp_path,
        "b",
    )
    assert [e["skill"] for e in slashed] == [e["skill"] for e in bare]


def test_unrecognised_payload_is_recorded_not_dropped(tmp_path: Path) -> None:
    """Silence is the bug this hook exists to prevent.

    An unknown payload shape must surface as an explicit marker so the gap is
    visible in telemetry, rather than looking identical to a skill nobody used.
    """
    events = _run({"hook_event_name": "UserPromptExpansion", "mystery": "x"}, tmp_path)
    assert len(events) == 1
    assert events[0]["skill"].startswith("invoke-unmapped")
    assert "UserPromptExpansion" in events[0]["skill"]


def test_empty_payload_does_not_invent_a_skill(tmp_path: Path) -> None:
    """An empty object is not a skill invocation; never fabricate a name."""
    events = _run({}, tmp_path)
    assert all(e["skill"].startswith("invoke-unmapped") for e in events)


@pytest.mark.skip(reason='Consumer Claude settings wiring belongs to A4/init integration')
def test_settings_wire_both_invocation_paths() -> None:
    """A correct hook script is useless if only one path is wired to it."""
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    hooks = settings.get("hooks", {})
    wired = {
        event
        for event, entries in hooks.items()
        for entry in entries
        for h in entry.get("hooks", [])
        if "skill-invoke-event.sh" in h.get("command", "")
    }
    assert "PostToolUse" in wired, "Skill-tool path not wired"
    assert "UserPromptExpansion" in wired, "slash-command path not wired"
