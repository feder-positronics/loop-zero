"""Injection, package isolation and preserved schema boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from loopzero.config import Profile
from loopzero.runners import bridge, claude, codex, claude_token, codex_credential, process
from loopzero.runners.contract import governed_result_schema
from loopzero.runners.registry import NATIVE_RUNTIME_REGISTRY
from loopzero.runners.settings import DEFAULT_SETTINGS, PACKAGED_BRIDGE, RuntimeSettings, get_settings


def test_legacy_names_and_defaults():
    settings = RuntimeSettings(env_prefix="INTELFLO")
    assert settings.env_name("CODEX_AUTH_FD") == "INTELFLO_CODEX_AUTH_FD"
    assert settings.temp_name("codex-cli") == "intelflo-codex-cli-"
    assert settings.memfd_name("claude-auth") == "intelflo-claude-auth"
    assert settings.lock_name("codex-refresh") == ".intelflo_codex_refresh.lock"
    assert settings.state_path(Path("/account")) == Path("/account/.local/state/intelflo")
    assert DEFAULT_SETTINGS.env_prefix == "LOOPZERO"
    assert DEFAULT_SETTINGS.bridge_path == PACKAGED_BRIDGE
    assert DEFAULT_SETTINGS.temp_name("cursor-cli") == "loopzero-cursor-cli-"


def test_profile_paths_and_refresh_command(tmp_path):
    python = tmp_path / "tools" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    profile = Profile(root=tmp_path, core_repository="example", core_revision="0" * 40,
                      core_path=Path("core"), profiles=(), checks={}, env_prefix="CUSTOM",
                      state_root=str(tmp_path / "state"), toolchain={"interpreter": "tools/venv/bin/python"})
    settings = RuntimeSettings.from_profile(profile)
    with settings.use():
        assert claude.repository_python(tmp_path) == python
        assert codex.repository_python(tmp_path / "tools") == python
        assert claude.sdk_bridge_path(tmp_path) == PACKAGED_BRIDGE
        assert codex_credential._default_refresh_command() == (str(python), "-I", str(PACKAGED_BRIDGE), "--codex-refresh")
        assert settings.state_path(Path("/irrelevant")) == tmp_path / "state"
    assert settings.tooling_root == tmp_path


def test_scopes_restore_after_errors_and_do_not_cross_threads():
    parent = get_settings()
    def names(prefix):
        with RuntimeSettings(env_prefix=prefix).use():
            with pytest.raises(RuntimeError):
                with RuntimeSettings(env_prefix="INNER").use():
                    raise RuntimeError()
            return get_settings().env_name("RUNTIME_PROGRESS_FD")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(names, ("ONE", "TWO"))) == ["ONE_RUNTIME_PROGRESS_FD", "TWO_RUNTIME_PROGRESS_FD"]
    assert get_settings() is parent


def test_adapter_retains_settings_and_registry_accepts_injection(tmp_path):
    settings = RuntimeSettings(env_prefix="CUSTOM", toolchain_interpreter=Path("vendor/python"))
    adapter = NATIVE_RUNTIME_REGISTRY.create("claude", settings=settings)
    seen = []
    adapter._sdk_available = lambda python, cwd: seen.append(python) or False
    adapter._which = lambda _: "/native"
    from .test_agent_runtimes import claude_request
    adapter.probe_sdk(claude_request(tmp_path))
    assert seen == [tmp_path / "vendor/python"]
    # Resolver calls execute inside the retained constructor scope.
    assert adapter._settings is settings
    with settings.use():
        assert claude.repository_python(tmp_path) == tmp_path / "vendor/python"
    assert get_settings() is not settings


def test_dynamic_environment_names_and_token_state(tmp_path, monkeypatch):
    with RuntimeSettings(env_prefix="CUSTOM", state_root=str(tmp_path / "state")).use():
        merged = process.merge_runtime_cache_environment({}, [("CUSTOM_OUTER_WORKER_SANDBOX", "1")], write_root=tmp_path)
        assert merged == {"CUSTOM_OUTER_WORKER_SANDBOX": "1"}
        filtered = process.filtered_child_environment({"CUSTOM_RUNTIME_PROGRESS_FD": "123", "KEEP": "yes"})
        assert filtered == {"KEEP": "yes"}
        monkeypatch.setattr(claude_token.pwd, "getpwuid", lambda _: type("Account", (), {"pw_dir": str(tmp_path)})())
        assert claude_token._default_token_path() == tmp_path / "state/claude-token"


@pytest.mark.parametrize("module", [False, True])
def test_bridge_invocations_without_sdk_or_worktree_shadow(tmp_path, module):
    marker = tmp_path / "shadow-imported"
    (tmp_path / "codex_isolation.py").write_text(f"open({str(marker)!r}, 'w').close()")
    command = ([sys.executable, "-m", "loopzero.runners.bridge"] if module else
               [sys.executable, "-I", str(PACKAGED_BRIDGE)])
    source_root = Path(__file__).resolve().parents[3] / "src"
    import os
    environment = {**os.environ, "PYTHONPATH": str(source_root)}
    result = subprocess.run(command, cwd=tmp_path, env=environment, input="invalid-json", text=True, capture_output=True)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"type": "error", "reason": "protocol"}
    assert result.stderr == ""
    assert not marker.exists()


def test_settings_cross_bridge_process_and_are_consumed(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text('import json, os\nprint(os.environ["LOOPZERO_RUNTIME_SETTINGS"])\n')
    settings = RuntimeSettings(env_prefix="CUSTOM", bridge_path=script, state_root=str(tmp_path / "state"))
    with settings.use():
        result = process.run_cli(settings.bridge_command(tmp_path), cwd=tmp_path, input_text="", timeout_s=2, env={})
    assert result.returncode == 0
    assert json.loads(result.stdout)["env_prefix"] == "CUSTOM"
    import os
    old = os.environ.get("LOOPZERO_RUNTIME_SETTINGS")
    try:
        os.environ.update(settings.child_environment())
        assert RuntimeSettings.from_environment() == settings
        assert "LOOPZERO_RUNTIME_SETTINGS" not in os.environ
    finally:
        if old is not None:
            os.environ["LOOPZERO_RUNTIME_SETTINGS"] = old


@pytest.mark.parametrize("paths,sections", [([], ["code"]), (["src/auth.py"], ["code", "security"])])
def test_governed_review_schema_preserves_section_contract(paths, sections):
    schema = governed_result_schema("task", task={"work_kind": "review", "required_sections": sections, "security_trigger_paths": paths})
    fragment = schema["properties"]["review_sections"]
    assert fragment["required"] == sections
    assert fragment["additionalProperties"] is False
    assert schema["properties"]["recommended_followups"]["maxItems"] == 0
    with pytest.raises(ValueError, match="required_sections must exactly match"):
        governed_result_schema("task", task={"required_sections": [], "security_trigger_paths": paths})


def test_base_import_does_not_import_vendor_sdks():
    source_root = Path(__file__).resolve().parents[3] / "src"
    code = f"import sys; sys.path.insert(0, {str(source_root)!r}); import loopzero.runners; import loopzero.runners.bridge; assert not any(n.startswith(('openai_codex', 'claude_agent_sdk')) for n in sys.modules)"
    result = subprocess.run([sys.executable, "-I", "-c", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
