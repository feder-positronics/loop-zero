import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    module_path = repo_root / "scripts" / "util" / "agent_event.py"
    spec = importlib.util.spec_from_file_location("agent_event", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def test_detect_harness_prefers_codex_env(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_HARNESS", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert module.detect_harness() == "codex"


def test_resolve_session_prefers_codex_thread_id(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-xyz")

    session_id, source = module.resolve_session(Path("/tmp/repo"), "feature/test")

    assert session_id == "thread-xyz"
    assert source == "codex_thread"
