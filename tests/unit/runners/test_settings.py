"""Injection, package isolation and preserved schema boundaries."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from loopzero.config import Profile
from loopzero.runners import bridge, claude, codex, containment, process
from loopzero.runners import claude as claude_token, codex as codex_credential
from loopzero.runners.contract import governed_result_schema
from loopzero.runners.registry import NATIVE_RUNTIME_REGISTRY
from loopzero.runners.settings import DEFAULT_SETTINGS, PACKAGED_BRIDGE, RuntimeSettings, get_settings


@pytest.fixture
def intelflo_containment_comparison_fixture():
    """Import the read-only IntelFlo original only for argv comparison."""
    path = Path("/home/marcin/dev/intelflo/scripts/util/agent_dispatch.py")
    spec = importlib.util.spec_from_file_location(
        "_intelflo_containment_comparison_fixture", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
                      state_root=str(tmp_path / "state"), state_root_explicit=True,
                      toolchain={"interpreter": "tools/venv/bin/python"})
    settings = RuntimeSettings.from_profile(profile)
    with settings.use():
        assert claude.repository_python(tmp_path) == python
        assert codex.repository_python(tmp_path / "tools") == python
        assert claude.sdk_bridge_path(tmp_path) == PACKAGED_BRIDGE
        assert codex_credential._default_refresh_command() == (str(python), "-I", str(PACKAGED_BRIDGE), "--codex-refresh")
        assert settings.state_path(Path("/irrelevant")) == tmp_path / "state"
    assert settings.tooling_root == tmp_path


def test_profile_env_prefix_derives_legacy_state_root_when_unset(tmp_path, monkeypatch):
    profile = Profile(
        root=tmp_path,
        core_repository="example",
        core_revision="0" * 40,
        core_path=Path("core"),
        profiles=(),
        checks={},
        env_prefix="INTELFLO",
    )
    settings = RuntimeSettings.from_profile(profile)
    assert settings.state_root == "~/.local/state/intelflo"
    assert settings.state_path(Path("/account")) == Path(
        "/account/.local/state/intelflo"
    )
    with settings.use():
        monkeypatch.setattr(
            claude.pwd,
            "getpwuid",
            lambda _: type("Account", (), {"pw_dir": "/account"})(),
        )
        assert claude._default_token_path() == Path(
            "/account/.local/state/intelflo/claude-token"
        )


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


def test_hostile_parent_authority_never_reaches_child(tmp_path, caplog):
    hostile_names = {
        "GH_TOKEN": "gh",
        "GITHUB_TOKEN": "github",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "WORKTREE_LEASE": "lease",
        "MIXED_NONCE_MARKER": "nonce",
        "UNLISTED_SECRET": "secret",
    }
    allowlist = frozenset({"PATH", "SAFE_MARKER", *set(hostile_names) - {"UNLISTED_SECRET"}})
    settings = RuntimeSettings(child_env_allowlist=allowlist)
    script = "import json, os; print(json.dumps(dict(os.environ), sort_keys=True))"
    with settings.use():
        result = process.run_cli(
            [sys.executable, "-I", "-c", script],
            cwd=tmp_path,
            input_text="",
            timeout_s=2,
            env={"PATH": os.environ["PATH"], "SAFE_MARKER": "kept", **hostile_names},
            unsandboxed=True,
            unsandboxed_reason="unit test isolates child-environment filtering",
        )
    child = json.loads(result.stdout)
    assert child["SAFE_MARKER"] == "kept"
    assert not set(hostile_names).intersection(child)
    assert "unit test isolates child-environment filtering" in caplog.text


def test_launch_cli_refuses_without_sandbox_wrapper(tmp_path):
    with pytest.raises(process.ProcessGroupError, match="requires a sandbox wrapper"):
        process.launch_cli([sys.executable, "-c", "pass"], cwd=tmp_path, env={})


def test_worker_isolated_command_keeps_git_metadata_read_only(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(containment, "_system_executable", lambda _: Path("/usr/bin/bwrap"))
    command = containment.worker_isolated_command(
        ["/usr/bin/python3", "-c", "pass"], writable_root=worktree
    )
    resolved = str(worktree.resolve())
    assert command[0] == "/usr/bin/bwrap"
    assert ["--bind", resolved, resolved] == command[
        command.index("--bind", command.index("--proc") + 1) :
        command.index("--bind", command.index("--proc") + 1) + 3
    ]
    git_binding = ["--ro-bind", f"{resolved}/.git", f"{resolved}/.git"]
    assert any(command[index : index + 3] == git_binding for index in range(len(command) - 2))
    assert command[-4:] == ["--", "/usr/bin/python3", "-c", "pass"]


def test_credential_binding_argv_exactly_matches_intelflo_original(
    tmp_path, monkeypatch, intelflo_containment_comparison_fixture
):
    original = intelflo_containment_comparison_fixture
    writable_root = tmp_path / "worktree"
    writable_root.mkdir()
    credential_path = writable_root / ".credentials" / "auth.json"
    credential_path.parent.mkdir()
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(
        containment, "_system_executable", lambda _: Path("/usr/bin/bwrap")
    )
    monkeypatch.setattr(containment, "_worker_runtime_read_roots", lambda _: ())
    monkeypatch.setattr(
        original, "system_executable", lambda _: Path("/usr/bin/bwrap")
    )
    monkeypatch.setattr(original, "_worker_runtime_read_roots", lambda _: ())
    arguments = {
        "writable_root": writable_root,
        "credential_bindings": ((37, credential_path),),
    }

    assert containment.worker_isolated_command(["runtime"], **arguments) == (
        original.worker_isolated_command(["runtime"], **arguments)
    )


def test_every_runner_subprocess_launch_is_owned_by_a_containment_seam():
    runners_root = Path(__file__).resolve().parents[3] / "src/loopzero/runners"
    launch_calls = set()

    class LaunchVisitor(ast.NodeVisitor):
        def __init__(self, filename):
            self.filename = filename
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            target = node.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                owner = target.value.id
                name = target.attr
                if (
                    owner == "subprocess"
                    and name in {"Popen", "run", "call", "check_call", "check_output"}
                ) or (owner == "os" and name.startswith("exec")):
                    launch_calls.add(
                        (
                            self.filename,
                            self.functions[-1] if self.functions else None,
                            f"{owner}.{name}",
                        )
                    )
            self.generic_visit(node)

    for source in runners_root.glob("*.py"):
        LaunchVisitor(source.name).visit(
            ast.parse(source.read_text(encoding="utf-8"))
        )

    assert launch_calls == {
        ("containment.py", "_exact_repair_file_bindings", "subprocess.run"),
        ("process.py", "launch_cli", "subprocess.Popen"),
    }


@pytest.mark.parametrize("module", [False, True])
def test_bridge_invocations_without_sdk_or_worktree_shadow(tmp_path, module):
    marker = tmp_path / "shadow-imported"
    (tmp_path / "codex_isolation.py").write_text(f"open({str(marker)!r}, 'w').close()")
    settings = RuntimeSettings(toolchain_interpreter=Path(sys.executable))
    command = ([sys.executable, "-m", "loopzero.runners.bridge"] if module else
               settings.bridge_command(tmp_path))
    source_root = Path(__file__).resolve().parents[3] / "src"
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
        result = process.run_cli(
            settings.bridge_command(tmp_path), cwd=tmp_path, input_text="",
            timeout_s=2, env={}, unsandboxed=True,
            unsandboxed_reason="unit test executes an isolated bridge fixture",
        )
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
