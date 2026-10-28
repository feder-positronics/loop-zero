"""Sanitized #9284/#9275 streams: native JSONL omits settings, never inspection."""
import dataclasses
import json
import shlex
from pathlib import Path

import pytest

from loopzero import cli, eligibility, github, runners
from tests.conftest import git
from tests.test_runners import (
    APPROVE,
    _argv,
    _claude_envelope,
    _fake_claude,
    _fake_codex,
    _review,
    fake_bwrap,  # noqa: F401
)


@pytest.mark.parametrize("number", [9284, 9275])
@pytest.mark.parametrize("mode", ["pinned", "default", "model-only", "effort-only", "wrong", "reported-conflict", "failed-read"])
def test_refused_stream_fresh_launch_records_only_explicit_flags(fake_bin, tmp_path, number, mode):
    git(tmp_path, "init", "-q")
    raw = json.loads((Path(__file__).parent / f"fixtures/codex-0165-refused-{number}.json").read_text())
    context = raw.pop("review_context")
    if mode == "failed-read":
        raw["events"][3]["item"]["exit_code"] = 1
    script = _fake_codex(fake_bin, raw["final_message"], version="codex-cli 0.165.0",
                         stderr="model: other-model" if mode == "reported-conflict" else "")
    text = script.read_text()
    start = text.index("echo '{\"type\":\"thread.started\"")
    script.write_text(text[:start] + "printf '%s\\n' " + shlex.quote(
        '\n'.join(json.dumps(e) for e in raw["events"])) + '\nexit 0\n')
    pins = {} if mode == "default" else {"model": "gpt-6-astra", "effort": "medium"}
    if mode in ("model-only", "effort-only"):
        pins.pop("effort" if mode == "model-only" else "model")
    if mode == "wrong":
        pins["effort"] = "high"
    native = runners._review_codex("material", policy="policy", cwd=tmp_path, head=context["head"],
        kind="primary", timeout=10, ro_paths=(), model=pins.get("model"), effort=pins.get("effort"),
        review_context={**context, "requested": {k: pins.get(k) for k in ("model", "effort")}})
    captured = json.loads(native.raw)
    call = runners.review_settings(native)[0]
    assert call["configured"] == {k: pins.get(k) for k in ("model", "effort")}
    assert call["reported"] == {"model": "other-model" if mode == "reported-conflict" else None,
                                "effort": None}
    argv = _argv(fake_bin, "codex")
    assert (argv[argv.index("-m") + 1] if "-m" in argv else None) == pins.get("model")
    assert [v for v in argv if v.startswith("model_reasoning_effort=")] == (
        [f"model_reasoning_effort={pins['effort']}"] if "effort" in pins else [])
    projection = runners.codex_evidence(native)
    assert runners.decode_codex_evidence(runners.encode_codex_evidence(projection)) == projection
    assert eligibility.recovery_inspection(projection, native.head) == (mode == "pinned")
    body = github._review_body(native, [], cli.review_marker(native.head, native.kind))
    assert f"configured model {pins.get('model', 'unknown')} (effort {pins.get('effort', 'unknown')})" in body
    # A retained receipt from before the fix cannot be upgraded from requested/model fields.
    captured.pop("configured_settings")
    legacy = dataclasses.replace(native, raw=json.dumps(captured), model="gpt-6-astra", effort="medium",
        provenance={**native.provenance, "configured_settings": [{"model": "gpt-6-astra", "effort": "medium"}]})
    assert not eligibility.recovery_inspection(runners.codex_evidence(legacy), native.head)


@pytest.mark.parametrize("pins", [{}, {"model": "pinned-model", "effort": "medium"}])
def test_claude_missing_effort_retains_launch_flags_through_chunks(fake_bin, tmp_path, pins):
    _fake_claude(fake_bin, _claude_envelope(APPROVE, modelUsage={"reported-model": {}}))
    first = _review("claude", tmp_path, **pins)
    _fake_claude(fake_bin, _claude_envelope(APPROVE, session_id="second"))
    merged = runners.merge_reviews([first, _review("claude", tmp_path, **pins)])
    expected = {k: pins.get(k) for k in ("model", "effort")}
    assert [c["configured"] for c in runners.review_settings(merged)] == [expected, expected]
    assert runners.review_settings(merged)[0]["reported"] == {"model": "reported-model", "effort": None}
    assert ("--effort" in _argv(fake_bin, "claude")) == bool(pins)
    body = github._review_body(first, [], "")
    assert f"configured model {pins.get('model', 'unknown')} (effort {pins.get('effort', 'unknown')})" in body


@pytest.mark.parametrize("configured", [None, {}, "forged", {"model": "other", "effort": "medium"}])
def test_malformed_or_inconsistent_launch_configuration_holds(configured):
    from tests.test_runners import _native_codex_result

    native = _native_codex_result("a" * 40)
    raw = json.loads(native.raw)
    raw["configured_settings"] = configured
    with pytest.raises(runners.RunnerBadOutput, match="invalid complete native result"):
        runners.codex_evidence(dataclasses.replace(native, raw=json.dumps(raw)))
