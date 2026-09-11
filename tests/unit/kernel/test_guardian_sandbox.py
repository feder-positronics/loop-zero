"""Focused tests for Guardian sandbox runtime-tool provenance."""

from __future__ import annotations

import base64
import errno
import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "src" / "loopzero" / "kernel" / "sandbox.py"


def load_module():
    return importlib.import_module('loopzero.kernel.sandbox')


module = load_module()


def _codex_credential(*, expires_at_s: int = 4_000_000_000) -> str:
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": expires_at_s}).encode())
        .rstrip(b"=")
        .decode()
    )
    token = f"header.{claims}.signature"
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": token,
                "id_token": token,
                "refresh_token": "refresh",
                "account_id": "account-1",
            },
        }
    )


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("worktree", "protected"),
    [
        (Path("/proc/loopzero/worktree"), Path("/home/user/protected")),
        (Path("/sys/loopzero/worktree"), Path("/home/user/protected")),
        (Path("/dev/loopzero/worktree"), Path("/home/user/protected")),
        (Path("/home/user/worktree"), Path("/proc/loopzero/protected")),
        (Path("/home/user/worktree"), Path("/sys/loopzero/protected")),
        (Path("/home/user/worktree"), Path("/dev/loopzero/protected")),
    ],
)
def test_sandbox_builder_rejects_kernel_filesystem_mounts(
    monkeypatch, worktree: Path, protected: Path
) -> None:
    monkeypatch.setattr(module, "_validated", lambda path, directory=None: Path(path))
    monkeypatch.setattr(module, "_tool", lambda name, **kwargs: Path(f"/usr/bin/{name}"))
    monkeypatch.setattr(module, "_system_tool", lambda name: Path(f"/usr/bin/{name}"))

    with pytest.raises(module.SandboxError, match="cannot be located"):
        module.command(
            ["/usr/bin/true"],
            worktree=worktree,
            writable_worktree=True,
            audit_source=None,
            audit_destination=None,
            git_source=None,
            git_destination=None,
            writable_git=False,
            read_only_roots=(protected,),
            deny_network=True,
        )


def test_validation_child_overlays_config_and_forces_safe_git_settings(
    tmp_path, monkeypatch
) -> None:
    destination = tmp_path / "repository-config"
    destination.write_text("candidate\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def validation_command(argv, **kwargs):
        mounts = kwargs["read_only_file_mounts"]
        assert len(mounts) == 1
        source, target = mounts[0]
        observed["payload"] = source.read_bytes()
        observed["mode"] = stat.S_IMODE(source.stat().st_mode)
        observed["destination"] = target
        return list(argv)

    def run(command, **kwargs):
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "validation_command", validation_command)
    monkeypatch.setattr(subprocess, "run", run)
    payload = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n"

    result = module.run_validation_child(
        ["/usr/bin/true"],
        worktree=tmp_path,
        git_config_overlays=((destination, payload),),
    )

    assert result.returncode == 0
    assert observed["payload"] == payload
    assert observed["mode"] == 0o444
    assert observed["destination"] == destination
    environment = observed["environment"]
    assert isinstance(environment, dict)
    configured = {
        environment[f"GIT_CONFIG_KEY_{index}"]:
        environment[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(environment["GIT_CONFIG_COUNT"]))
    }
    assert configured["core.hooksPath"] == "/dev/null"
    assert configured["core.fsmonitor"] == "false"
    assert configured["core.sshCommand"] == ""
    assert configured["core.pager"] == "cat"
    assert configured["core.editor"] == "false"
    assert configured["core.worktree"] == ""


def test_codex_subscription_credential_opens_and_closes_owner_only_file():
    from contextlib import contextmanager
    observed = []
    @contextmanager
    def broker(*, requested_runtime_s):
        observed.append(requested_runtime_s)
        import tempfile
        handle = tempfile.TemporaryFile()
        fd = handle.fileno()
        os.write(fd, _codex_credential().encode())
        try:
            yield fd
        finally:
            handle.close()
    with module.codex_subscription_credential(
        requested_runtime_s=900, credential_broker=broker
    ) as descriptor:
        snapshot = json.loads(os.pread(descriptor, 1024 * 1024, 0))
        assert snapshot["tokens"]["refresh_token"] == snapshot["tokens"]["access_token"]
        assert snapshot["tokens"]["refresh_token"] != "refresh"
        assert os.fstat(descriptor).st_mode & 0o777 == 0o400
        if hasattr(os, "memfd_create"):
            seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
            assert seals & fcntl.F_SEAL_WRITE
            assert seals & fcntl.F_SEAL_GROW
            assert seals & fcntl.F_SEAL_SHRINK
            assert seals & fcntl.F_SEAL_SEAL
    assert observed == [900]
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("unsafe_kind", ["type", "pipe", "empty", "invalid-json"])
def test_codex_subscription_credential_rejects_unsafe_file(unsafe_kind):
    from contextlib import contextmanager
    @contextmanager
    def broker(**kwargs):
        if unsafe_kind == "type":
            yield "not-a-descriptor"
            return
        read_fd, write_fd = os.pipe()
        if unsafe_kind == "pipe":
            path = Path(f"/proc/self/fd/{read_fd}")
            assert path.exists()
            os.close(write_fd)
            try:
                yield read_fd
            finally:
                os.close(read_fd)
            return
        import tempfile
        handle = tempfile.TemporaryFile()
        if unsafe_kind == "invalid-json":
            handle.write(b"not-json")
        try:
            yield handle.fileno()
        finally:
            handle.close()
    with pytest.raises(module.UnsafeCredentialError):
        with module.codex_subscription_credential(
            requested_runtime_s=900, credential_broker=broker
        ):
            pass


def test_codex_subscription_credential_rejects_world_readable_regular_file(tmp_path):
    from contextlib import contextmanager

    credential = tmp_path / "credential.json"
    credential.write_text(_codex_credential(), encoding="utf-8")
    credential.chmod(0o644)

    @contextmanager
    def broker(**kwargs):
        descriptor = os.open(credential, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    with pytest.raises(module.UnsafeCredentialError, match="unsafe"):
        with module.codex_subscription_credential(
            requested_runtime_s=900, credential_broker=broker
        ):
            pass


def test_codex_subscription_credential_requires_the_callers_runtime():
    with pytest.raises(TypeError):
        with module.codex_subscription_credential():
            pass
    with pytest.raises(module.SandboxError, match="credential_broker"):
        with module.codex_subscription_credential(requested_runtime_s=900):
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


@pytest.mark.parametrize("name", ["node", "corepack", "uv"])
@pytest.mark.parametrize("path_alias", [False, True])
@pytest.mark.parametrize("path_index", [0, 1])
def test_runtime_tool_rejects_writable_path_symlink_to_external_target(
    monkeypatch, tmp_path: Path, name: str, path_alias: bool, path_index: int
) -> None:
    worktree = tmp_path / "worktree"
    bin_dir = worktree / "bin"
    bin_dir.mkdir(parents=True)
    target = tmp_path / "scratch" / name
    _write_executable(target)
    (bin_dir / name).symlink_to(target)
    search_dir = bin_dir
    if path_alias:
        search_dir = tmp_path / "alias"
        search_dir.symlink_to(bin_dir, target_is_directory=True)
    entries = [str(tmp_path / "empty-bin")] * path_index + [str(search_dir)]
    monkeypatch.setenv("PATH", os.pathsep.join(entries))

    with pytest.raises(module.SandboxError, match="writable sandbox source"):
        module._tool(name, forbidden_roots=(worktree,))


@pytest.mark.parametrize("name", ["uv", "node", "corepack"])
@pytest.mark.parametrize("writable_cache", [False, True])
def test_hosted_toolcache_outer_permissions_follow_sandbox_mount_authority(
    monkeypatch, tmp_path: Path, name: str, writable_cache: bool
) -> None:
    cache = tmp_path / "opt" / "hostedtoolcache"
    target = cache / name / "1.0" / "aarch64" / name
    _write_executable(target)
    target.parent.chmod(0o755)
    cache.chmod(0o777)
    monkeypatch.setenv("PATH", f"{tmp_path / 'empty-bin'}:{target.parent}")

    if writable_cache:
        with pytest.raises(module.SandboxError, match="writable sandbox source"):
            module._tool(name, forbidden_roots=(cache,))
    else:
        assert module._tool(name) == target.resolve()


@pytest.mark.parametrize("name", ["uv", "node", "corepack"])
@pytest.mark.parametrize("directory_hop", [False, True])
@pytest.mark.parametrize("relative_hop", [False, True])
def test_runtime_tool_rejects_intermediate_writable_symlink_hop(
    monkeypatch, tmp_path: Path, name: str, directory_hop: bool, relative_hop: bool
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = tmp_path / "host-tools" / name
    _write_executable(target)
    hop = worktree / "hop"
    hop_target = target.parent if directory_hop else target
    hop.symlink_to(
        os.path.relpath(hop_target, hop.parent) if relative_hop else hop_target
    )
    entry = tmp_path / "host-bin" / ("alias" if directory_hop else name)
    entry.parent.mkdir()
    entry.symlink_to(os.path.relpath(hop, entry.parent) if relative_hop else hop)
    monkeypatch.setenv("PATH", str(entry if directory_hop else entry.parent))

    with pytest.raises(module.SandboxError, match="writable sandbox source"):
        module._tool(name, forbidden_roots=(worktree,))


def test_runtime_tool_accepts_host_relative_symlink_chain(monkeypatch, tmp_path: Path):
    target = tmp_path / "host-tools" / "uv"
    _write_executable(target)
    intermediate = tmp_path / "hop"
    intermediate.symlink_to("host-tools/uv")
    entry = tmp_path / "bin" / "uv"
    entry.parent.mkdir()
    entry.symlink_to("../hop")
    monkeypatch.setenv("PATH", str(entry.parent))

    assert module._tool("uv", forbidden_roots=(tmp_path / "worktree",)) == target


def test_runtime_tool_symlink_traversal_is_bounded(tmp_path: Path):
    link = tmp_path / "loop"
    link.symlink_to("loop")

    with pytest.raises(module.SandboxError, match="too many symlink hops"):
        module._protected_tool_path(link, forbidden_roots=())


@pytest.mark.parametrize("unsafe_component", ["executable", "parent"])
def test_runtime_tool_rejects_unprotected_external_target(
    monkeypatch, tmp_path: Path, unsafe_component: str
) -> None:
    target = tmp_path / "host-tools" / "node"
    _write_executable(target)
    component = target if unsafe_component == "executable" else target.parent
    component.chmod(0o777)
    monkeypatch.setenv("PATH", str(target.parent))

    with pytest.raises(module.SandboxError, match="not protected"):
        module._tool("node")


@pytest.mark.parametrize(
    "group_kind", ["private", "supplementary", "primary", "unknown"]
)
@pytest.mark.parametrize("writable_component", ["executable", "parent"])
def test_runtime_tool_group_write_requires_owner_private_membership(
    monkeypatch, tmp_path: Path, group_kind: str, writable_component: str
) -> None:
    target = tmp_path / "host-tools" / "node"
    _write_executable(target)
    target.parent.chmod(0o755)
    component = target if writable_component == "executable" else target.parent
    component.chmod(0o775)
    group_id = component.stat().st_gid
    owner = SimpleNamespace(pw_name="operator", pw_gid=group_id)
    users = [owner]
    if group_kind == "primary":
        users.append(SimpleNamespace(pw_name="other", pw_gid=group_id))

    def lookup_group(_group_id: int):
        if group_kind == "unknown":
            raise KeyError(_group_id)
        members = (
            ["operator", "other"] if group_kind == "supplementary" else ["operator"]
        )
        return SimpleNamespace(gr_mem=members)

    monkeypatch.setattr(module.pwd, "getpwuid", lambda _uid: owner)
    monkeypatch.setattr(module.pwd, "getpwall", lambda: users)
    monkeypatch.setattr(module.grp, "getgrgid", lookup_group)
    monkeypatch.setenv("PATH", str(target.parent))

    if group_kind == "private":
        assert module._tool("node") == target.resolve()
    else:
        with pytest.raises(module.SandboxError, match="not protected"):
            module._tool("node")


@pytest.mark.parametrize("acl_state", ["entries", "unsupported", "unreadable"])
@pytest.mark.parametrize("writable_component", ["executable", "parent"])
def test_runtime_tool_private_group_write_fails_closed_on_acl_entries(
    monkeypatch, tmp_path: Path, acl_state: str, writable_component: str
) -> None:
    target = tmp_path / "host-tools" / "node"
    _write_executable(target)
    target.parent.chmod(0o755)
    component = target if writable_component == "executable" else target.parent
    component.chmod(0o775)
    monkeypatch.setattr(module, "_private_group", lambda _group_id: True)

    def listxattr(path, *, follow_symlinks: bool = True) -> list[str]:
        if Path(path) != component:
            return []
        if acl_state == "unsupported":
            raise OSError(errno.ENOTSUP, "xattr unsupported")
        if acl_state == "unreadable":
            raise OSError(errno.EACCES, "xattr unreadable")
        return ["system.posix_acl_access"]

    monkeypatch.setattr(module.os, "listxattr", listxattr)
    monkeypatch.setenv("PATH", str(target.parent))

    if acl_state == "unsupported":
        assert module._tool("node") == target.resolve()
    else:
        with pytest.raises(module.SandboxError, match="not protected"):
            module._tool("node")


@pytest.mark.parametrize("unsafe_root", [None, "runtime", "cache"])
def test_trusted_corepack_mounts_its_adjacent_node_runtime(
    monkeypatch, tmp_path: Path, unsafe_root: str | None
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

    if unsafe_root is not None:
        (runtime if unsafe_root == "runtime" else corepack_home).chmod(0o777)
        with pytest.raises(module.SandboxError, match="not protected"):
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
        return

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
