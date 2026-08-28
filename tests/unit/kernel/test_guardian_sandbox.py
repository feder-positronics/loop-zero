"""Focused tests for Guardian sandbox runtime-tool provenance."""

from __future__ import annotations

import importlib.util
import os
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


def test_codex_subscription_credential_opens_and_closes_owner_only_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    auth.chmod(0o600)
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)

    with module.codex_subscription_credential() as descriptor:
        assert os.pread(descriptor, auth.stat().st_size, 0) == auth.read_bytes()

    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode", "empty"])
def test_codex_subscription_credential_rejects_unsafe_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsafe_kind: str
) -> None:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    if unsafe_kind == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
        target.chmod(0o600)
        auth.symlink_to(target)
    else:
        auth.write_text("" if unsafe_kind == "empty" else '{"auth_mode":"chatgpt"}')
        auth.chmod(0o644 if unsafe_kind == "mode" else 0o600)
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)

    with pytest.raises(module.UnsafeCredentialError):
        with module.codex_subscription_credential():
            pass


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


def test_corepack_cannot_come_from_writable_worktree(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    corepack = worktree / "bin" / "corepack"
    uv = tmp_path / "host-tools" / "uv"
    _write_executable(corepack)
    _write_executable(uv)
    monkeypatch.setenv("PATH", f"{corepack.parent}:{uv.parent}")
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    with pytest.raises(module.SandboxError, match="writable sandbox source: corepack"):
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
            include_corepack_runtime=True,
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


def test_trusted_corepack_mounts_its_adjacent_node_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    runtime = tmp_path / "trusted-node"
    corepack_home = tmp_path / "corepack-home"
    corepack = runtime / "lib" / "node_modules" / "corepack" / "dist" / "corepack.js"
    node = runtime / "bin" / "node"
    uv = tmp_path / "host-tools" / "uv"
    worktree.mkdir()
    corepack_home.mkdir()
    _write_executable(corepack)
    _write_executable(node)
    _write_executable(uv)
    (runtime / "bin" / "corepack").symlink_to(corepack)
    monkeypatch.setenv("PATH", f"{runtime / 'bin'}:{uv.parent}")
    monkeypatch.setenv("COREPACK_HOME", str(corepack_home))
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    argv = module.command(
        ["/usr/bin/bash", "-c", "corepack pnpm test"],
        worktree=worktree,
        writable_worktree=True,
        audit_source=None,
        audit_destination=None,
        git_source=None,
        git_destination=None,
        writable_git=False,
        deny_network=True,
        include_model_runtime=False,
        include_corepack_runtime=True,
    )

    assert ["--ro-bind", str(runtime), str(module.GUARDIAN_NODE_ROOT)] == argv[
        argv.index(str(runtime)) - 1 : argv.index(str(runtime)) + 2
    ]
    assert [
        "--ro-bind",
        str(corepack_home),
        str(module.GUARDIAN_COREPACK_HOME),
    ] == argv[argv.index(str(corepack_home)) - 1 : argv.index(str(corepack_home)) + 2]
    path_index = argv.index("PATH")
    assert argv[path_index - 1 : path_index + 2] == [
        "--setenv",
        "PATH",
        f"{module.GUARDIAN_NODE_ROOT / 'bin'}:/run/guardian-bin:/usr/bin:/bin",
    ]
    home_index = argv.index("COREPACK_HOME")
    assert argv[home_index - 1 : home_index + 2] == [
        "--setenv",
        "COREPACK_HOME",
        str(module.GUARDIAN_COREPACK_HOME),
    ]


def test_corepack_without_an_owning_node_distribution_is_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    host_tools = tmp_path / "host-tools"
    worktree.mkdir()
    _write_executable(host_tools / "corepack")
    _write_executable(host_tools / "uv")
    monkeypatch.setenv("PATH", str(host_tools))
    monkeypatch.delenv("COREPACK_HOME", raising=False)
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    with pytest.raises(module.SandboxError, match="Corepack runtime is unavailable"):
        module.command(
            ["/usr/bin/bash", "-c", "corepack pnpm test"],
            worktree=worktree,
            writable_worktree=True,
            audit_source=None,
            audit_destination=None,
            git_source=None,
            git_destination=None,
            writable_git=False,
            deny_network=True,
            include_model_runtime=False,
            include_corepack_runtime=True,
        )


def test_corepack_without_a_trusted_home_is_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    runtime = tmp_path / "trusted-node"
    empty_home = tmp_path / "empty-home"
    corepack = runtime / "lib" / "node_modules" / "corepack" / "dist" / "corepack.js"
    node = runtime / "bin" / "node"
    uv = tmp_path / "host-tools" / "uv"
    worktree.mkdir()
    empty_home.mkdir()
    _write_executable(corepack)
    _write_executable(node)
    _write_executable(uv)
    (runtime / "bin" / "corepack").symlink_to(corepack)
    monkeypatch.setenv("PATH", f"{runtime / 'bin'}:{uv.parent}")
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.delenv("COREPACK_HOME", raising=False)
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    with pytest.raises(module.SandboxError, match="Corepack home is unavailable"):
        module.command(
            ["/usr/bin/bash", "-c", "corepack pnpm test"],
            worktree=worktree,
            writable_worktree=True,
            audit_source=None,
            audit_destination=None,
            git_source=None,
            git_destination=None,
            writable_git=False,
            deny_network=True,
            include_model_runtime=False,
            include_corepack_runtime=True,
        )


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
