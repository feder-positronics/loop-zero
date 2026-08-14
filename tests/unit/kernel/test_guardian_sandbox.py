"""Focused tests for Guardian sandbox runtime-tool provenance."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "util" / "guardian_sandbox.py"


def load_module():
    spec = importlib.util.spec_from_file_location("guardian_sandbox_test", SCRIPT)
    assert spec and spec.loader
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "util"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_runtime_tool_cannot_come_from_writable_worktree(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    uv = worktree / "bin" / "uv"
    _write_executable(uv)
    monkeypatch.setenv("PATH", str(uv.parent))
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    with pytest.raises(module.SandboxError, match="writable sandbox source"):
        module.command(
            ["/usr/bin/true"],
            worktree=worktree,
            writable_worktree=True,
            audit_source=None,
            audit_destination=None,
            git_source=None,
            git_destination=None,
            writable_git=False,
            deny_network=True,
            include_model_runtime=False,
        )


def test_external_user_owned_runtime_tool_remains_valid(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    uv = tmp_path / "host-tools" / "uv"
    _write_executable(uv)
    monkeypatch.setenv("PATH", str(uv.parent))
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    argv = module.command(
        ["/usr/bin/true"],
        worktree=worktree,
        writable_worktree=True,
        audit_source=None,
        audit_destination=None,
        git_source=None,
        git_destination=None,
        writable_git=False,
        deny_network=True,
        include_model_runtime=False,
    )

    assert str(uv) in argv


def test_candidate_python_symlink_cannot_select_a_host_mount(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    host_config = tmp_path / "host-home" / ".config"
    credential = host_config / "gh" / "hosts.yml"
    credential.parent.mkdir(parents=True)
    credential.write_text("secret\n", encoding="utf-8")
    candidate_python = worktree / "fastapi_backend" / ".venv" / "bin" / "python"
    candidate_python.parent.mkdir(parents=True)
    candidate_python.symlink_to(credential)
    uv = tmp_path / "host-tools" / "uv"
    _write_executable(uv)
    monkeypatch.setenv("PATH", str(uv.parent))
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    argv = module.command(
        ["/usr/bin/true"],
        worktree=worktree,
        writable_worktree=True,
        audit_source=None,
        audit_destination=None,
        git_source=None,
        git_destination=None,
        writable_git=False,
        deny_network=True,
        include_model_runtime=False,
    )

    assert str(host_config) not in argv


def test_trusted_read_only_venv_can_mount_its_external_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    primary_venv = tmp_path / "trusted" / ".venv"
    runtime = tmp_path / "trusted-runtime"
    runtime_python = runtime / "bin" / "python"
    _write_executable(runtime_python)
    primary_python = primary_venv / "bin" / "python"
    primary_python.parent.mkdir(parents=True)
    primary_python.symlink_to(runtime_python)
    uv = tmp_path / "host-tools" / "uv"
    _write_executable(uv)
    monkeypatch.setenv("PATH", str(uv.parent))
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    argv = module.command(
        [str(worktree / "fastapi_backend" / ".venv" / "bin" / "python")],
        worktree=worktree,
        writable_worktree=True,
        audit_source=None,
        audit_destination=None,
        git_source=None,
        git_destination=None,
        writable_git=False,
        read_only_mounts=((primary_venv, worktree / "fastapi_backend" / ".venv"),),
        deny_network=True,
        include_model_runtime=False,
    )

    assert ["--ro-bind", str(runtime), str(runtime)] == argv[
        argv.index(str(runtime)) - 1 : argv.index(str(runtime)) + 2
    ]
