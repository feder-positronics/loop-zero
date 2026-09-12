"""Behavioral security specifications for host-authenticated validation."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from loopzero.kernel import authority, capabilities, validation
from loopzero.kernel.settings import KernelSettings

from .capabilities import NAMESPACE_AVAILABLE, NAMESPACE_REASON
from .package_environment import package_environment

JOB_SH = Path(__file__).resolve().parents[3] / "src/loopzero/kernel/job.sh"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(root), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null", *args],
        text=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).strip()


def _signer() -> authority.CoordinatorAuthority:
    ephemeral = authority.TerminalAuthority.generate()
    return authority.CoordinatorAuthority(ephemeral.public_key, ephemeral._private_key)


def _workflow(commands: tuple[str, ...], hook: str = "acceptance") -> str:
    encoded = ", ".join(json.dumps(command) for command in commands)
    return (
        "profiles = []\n"
        "[core]\nrepository = \"https://example.invalid/core\"\n"
        f"revision = \"{'0' * 40}\"\npath = \"vendor/core\"\n"
        "[checks]\nrequired = []\nadvisory = []\nscheduled = []\n"
        f"[hooks]\n{hook} = [{encoded}]\n"
    )


def _repo(tmp_path: Path, commands: tuple[str, ...], hook: str = "acceptance") -> tuple[Path, str]:
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / "workflow.toml").write_text(_workflow(commands, hook), encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _actual(repo: Path, commands: tuple[str, ...], extra: tuple[str, ...] = ()) -> list[str]:
    rows = []
    for command in commands:
        argv = shlex.split(command)
        token = argv[0]
        executable = (
            str((repo / token).resolve())
            if "/" in token and not Path(token).is_absolute()
            else token if Path(token).is_absolute() else str(Path("/usr/bin") / token)
        )
        rows.append(shlex.join([executable, *argv[1:], *extra]))
    return rows


def _artifact(path: Path, signer: authority.CoordinatorAuthority, *, task: str,
              base: str, head: str, hook: str, commands: list[str],
              source_kind: str = "commit", source_sha: str | None = None,
              source_filesystem_sha256: str = "0" * 64,
              **changes) -> None:
    row = {
        "schema_version": validation.RESULT_SCHEMA, "task_id": task,
        "base_sha": base, "head_sha": head, "hook": hook,
        "source_kind": source_kind, "source_sha": source_sha or head,
        "source_filesystem_sha256": source_filesystem_sha256,
        "hook_commands": commands, "status": "completed", "exit_code": 0,
    }
    row.update(changes)
    path.write_text(json.dumps(signer.seal(row, authority_kind="coordinator")), encoding="utf-8")


def _environment(tmp_path: Path, timeout: float = 5) -> dict[str, str]:
    uv = shutil.which("uv")
    assert uv is not None
    settings = KernelSettings(env_prefix="INTELFLO", state_root=tmp_path / "state",
                              toolchain={"validation_timeout_s": timeout})
    return package_environment({
        "PATH": f"{Path(uv).parent}:/usr/bin:/bin",
        **settings.child_environment(),
    })


def _run(repo: Path, base: str, artifact: Path, key: Path, *, task: str = "task-1",
         hook: str = "acceptance", extra: tuple[str, ...] = (), timeout: float = 10,
         env: dict[str, str] | None = None,
         allow_dirty_tree: bool = False) -> subprocess.CompletedProcess[str]:
    dirty_arguments = ["--allow-dirty-tree"] if allow_dirty_tree else []
    return subprocess.run(
        [str(JOB_SH), "consumer-hook", "--base", base, "--head", base,
         "--task-id", task, "--result-artifact", str(artifact),
         "--coordinator-public-key", str(key), "--hook", hook,
         *dirty_arguments, "--", *extra],
        cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
    )


def _signed_case(tmp_path: Path, commands: tuple[str, ...] = ("gnutrue",)):
    repo, sha = _repo(tmp_path, commands)
    signer = _signer()
    key = tmp_path / "coordinator.der"
    key.write_bytes(signer.public_key)
    result = tmp_path / "result.json"
    source = validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)
    _artifact(
        result, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, commands),
        source_filesystem_sha256=source.filesystem_sha256,
    )
    return repo, sha, signer, key, result


def _script_case(tmp_path: Path, body: str, *, name: str = "collector"):
    commands = (f"./{name}",)
    repo, _ = _repo(tmp_path, commands)
    script = repo / name
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add collector")
    sha = _git(repo, "rev-parse", "HEAD")
    signer = _signer()
    key = tmp_path / "coordinator.der"
    key.write_bytes(signer.public_key)
    artifact = tmp_path / "result.json"
    source = validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)
    _artifact(
        artifact, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, commands),
        source_filesystem_sha256=source.filesystem_sha256,
    )
    return repo, sha, signer, key, artifact


def _direct_child(argv, *, worktree, timeout, **kwargs):
    return subprocess.run(
        argv, cwd=worktree, capture_output=True, text=True,
        timeout=timeout, start_new_session=True,
    )


def _source(sha: str) -> validation.SourceIdentity:
    return validation.SourceIdentity("commit", sha, "0" * 64)


def test_signed_result_accepts_real_coordinator_signature(tmp_path):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    source = validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)
    assert validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
        task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance",
        hook_commands=_actual(repo, ("gnutrue",)), source=source)["status"] == "completed"


def test_final_ci_repro_refuses_an_unsigned_terminal_artifact(tmp_path):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    result.write_text('{"status":"completed"}', encoding="utf-8")
    with pytest.raises(validation.UnsignedResultError):
        validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)), source=_source(sha))


def test_signed_result_rejects_real_tampering(tmp_path):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    row = json.loads(result.read_text()); row["status"] = "failed"
    result.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(validation.TamperedResultError):
        validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)), source=_source(sha))


@pytest.mark.parametrize("field,value", [("task_id", "other"), ("base_sha", "f" * 40),
                                          ("head_sha", "e" * 40), ("hook_commands", ["false"]),
                                          ("source_filesystem_sha256", "f" * 64)])
def test_signed_result_rejects_unbound_fields(tmp_path, field, value):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    _artifact(result, signer, task="task-1", base=sha, head=sha, hook="acceptance",
              commands=_actual(repo, ("gnutrue",)), **{field: value})
    with pytest.raises(validation.UnboundResultError, match=field):
        validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)), source=_source(sha))


def test_signed_final_ci_repro_rejects_authority_path_substitution(tmp_path):
    repo, sha, trusted, key, result = _signed_case(tmp_path)
    pinned = validation.read_coordinator_public_key(key)
    attacker = _signer(); key.write_bytes(attacker.public_key)
    _artifact(result, attacker, task="task-1", base=sha, head=sha, hook="acceptance", commands=_actual(repo, ("gnutrue",)))
    with pytest.raises(validation.TamperedResultError):
        validation.verify_result_artifact(result, coordinator_public_key=pinned,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)), source=_source(sha))


def test_verifier_provider_failure_is_retryable_not_tampering(
    tmp_path, monkeypatch, capsys
):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)

    def unavailable(*args, **kwargs):
        raise authority.TerminalAuthorityOperationalError("provider timed out")

    monkeypatch.setattr(validation, "verify_terminal_authority", unavailable)
    code = validation.main([
        "--worktree", str(repo), "--base", sha, "--head", sha,
        "--task-id", "task-1", "--result-artifact", str(artifact),
        "--coordinator-public-key", str(key), "--hook", "acceptance",
    ])

    assert code == 2
    assert "verifier is unavailable" in capsys.readouterr().err


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_verifier_provider_failure_is_operational_through_job_main(tmp_path):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    empty_allowlist = tmp_path / "empty-allowlist"
    empty_allowlist.mkdir()
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import loopzero.kernel.authority as authority\n"
        "from loopzero.trust import TrustedExecutableError, resolve_executable\n"
        f"allowlist = (Path({str(empty_allowlist)!r}),)\n"
        "def trusted(name):\n"
        "    found = resolve_executable(Path.cwd(), name, allowlist)\n"
        "    if found is None:\n"
        "        raise TrustedExecutableError(f'trusted {name} executable is unavailable')\n"
        "    return found\n"
        "authority.system_executable = trusted\n"
        "authority._openssl.cache_clear()\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "providerless-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(bootstrap))}:{shlex.quote(str(Path(__file__).resolve().parents[3] / 'src'))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = _environment(tmp_path)
    environment["LOOPZERO_PYTHON"] = str(wrapper)

    result = subprocess.run(
        [
            str(JOB_SH),
            "consumer-hook",
            "--base",
            sha,
            "--head",
            sha,
            "--task-id",
            "task-1",
            "--result-artifact",
            str(artifact),
            "--coordinator-public-key",
            str(key),
            "--hook",
            "acceptance",
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "validation result verifier is unavailable" in result.stderr
    assert "trusted Ed25519 provider is unavailable" in result.stderr
    assert "rejected (unsigned)" not in result.stderr
    assert "rejected (signature)" not in result.stderr
    assert "rejected (binding)" not in result.stderr


def test_explicit_base_ignores_ambient_serialized_redirect(tmp_path, monkeypatch):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    monkeypatch.setenv("LOOPZERO_KERNEL_SETTINGS", json.dumps({"toolchain": {"approved_base": "refs/heads/attacker"}}))
    assert validation.resolve_base(repo, base=sha) == sha


def test_validation_rejects_a_wrong_checkout_before_launch(tmp_path, monkeypatch):
    repo, approved, signer, key, artifact = _signed_case(tmp_path)
    _git(repo, "commit", "--allow-empty", "-qm", "other checkout")
    launched = False

    def child(*args, **kwargs):
        nonlocal launched
        launched = True
        return _direct_child(*args, **kwargs)

    monkeypatch.setattr(validation, "run_validation_child", child)
    with pytest.raises(validation.ValidationHookError, match="approved head"):
        validation.run_hook(
            worktree=repo, hook="acceptance", base_sha=approved,
            head_sha=approved, task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[],
        )
    assert not launched


def test_validation_rejects_head_movement_after_child_exit(tmp_path, monkeypatch):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)

    def move_head(argv, **kwargs):
        _git(repo, "commit", "--allow-empty", "-qm", "move during validation")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(validation, "run_validation_child", move_head)
    with pytest.raises(validation.UnboundResultError, match="source changed"):
        validation.run_hook(
            worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[],
        )


def test_validation_rejects_an_unapproved_dirty_tree(tmp_path, monkeypatch):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    (repo / "candidate-change").write_text("dirty\n", encoding="utf-8")
    launched = False

    def child(*args, **kwargs):
        nonlocal launched
        launched = True
        return _direct_child(*args, **kwargs)

    monkeypatch.setattr(validation, "run_validation_child", child)
    with pytest.raises(validation.ValidationHookError, match="allow-dirty-tree"):
        validation.run_hook(
            worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[],
        )
    assert not launched


def test_validation_accepts_a_tree_hash_bound_dirty_candidate(tmp_path, monkeypatch):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    candidate = repo / "candidate-change"
    candidate.write_text("dirty\n", encoding="utf-8")
    source = validation.source_identity(
        repo, approved_head=sha, allow_dirty_tree=True
    )
    assert source.kind == "tree"
    _artifact(
        artifact, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, ("gnutrue",)),
        source_kind=source.kind, source_sha=source.sha,
        source_filesystem_sha256=source.filesystem_sha256,
    )
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)

    assert validation.run_hook(
        worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
        task_id="task-1", result_artifact=artifact,
        coordinator_public_key=signer.public_key, extra=[],
        allow_dirty_tree=True,
    ) == 0


def test_validation_rejects_hostile_local_git_config_without_executing_it(
    tmp_path, monkeypatch
):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    marker = tmp_path / "hostile-git-ran"
    hostile = repo / ".git" / "hostile-git"
    hostile.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nprintf 'changed\\n'\n",
        encoding="utf-8",
    )
    hostile.chmod(0o755)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "config",
            "--local",
            "core.fsmonitor",
            str(hostile),
        ],
        check=True,
    )
    launched = False

    def child(*args, **kwargs):
        nonlocal launched
        launched = True
        return _direct_child(*args, **kwargs)

    monkeypatch.setattr(validation, "run_validation_child", child)
    with pytest.raises(validation.ValidationHookError, match="configuration is unsafe"):
        validation.run_hook(
            worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[],
        )
    assert not launched
    assert not marker.exists()


def test_validation_detects_an_empty_directory_created_by_the_child(
    tmp_path, monkeypatch
):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)

    def add_empty_directory(argv, **kwargs):
        (repo / "untracked-empty").mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(validation, "run_validation_child", add_empty_directory)
    with pytest.raises(validation.UnboundResultError, match="source changed"):
        validation.run_hook(
            worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[],
        )


def test_source_identity_rejects_a_symlinked_git_path_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    repo, sha = _repo(real, ("gnutrue",))
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(validation.ValidationHookError, match="symlinked component"):
        validation.source_identity(
            alias / repo.name, approved_head=sha, allow_dirty_tree=False
        )


def test_source_identity_rejects_a_symlinked_gitdir_ancestor(tmp_path):
    repo, sha = _repo(tmp_path, ("gnutrue",))
    metadata_parent = tmp_path / "metadata"
    metadata_parent.mkdir()
    moved_git = metadata_parent / "git-dir"
    (repo / ".git").rename(moved_git)
    alias = repo / "metadata-alias"
    alias.symlink_to(metadata_parent, target_is_directory=True)
    (repo / ".git").write_text(
        "gitdir: metadata-alias/git-dir\n", encoding="utf-8"
    )

    with pytest.raises(validation.ValidationHookError, match="symlinked component"):
        validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)


def test_source_identity_rejects_special_filesystem_nodes(tmp_path):
    repo, sha = _repo(tmp_path, ("gnutrue",))
    os.mkfifo(repo / "candidate-fifo")

    with pytest.raises(validation.ValidationHookError, match="special node"):
        validation.source_identity(repo, approved_head=sha, allow_dirty_tree=True)


def test_source_identity_distinguishes_independent_files_from_hard_links(tmp_path):
    repo, _ = _repo(tmp_path, ("gnutrue",))
    first = repo / "first.txt"
    second = repo / "second.txt"
    first.write_text("same bytes\n", encoding="utf-8")
    second.write_text("same bytes\n", encoding="utf-8")
    _git(repo, "add", "first.txt", "second.txt")
    _git(repo, "commit", "-qm", "add equal files")
    sha = _git(repo, "rev-parse", "HEAD")

    independent = validation.source_identity(
        repo, approved_head=sha, allow_dirty_tree=False
    )
    second.unlink()
    os.link(first, second)
    hardlinked = validation.source_identity(
        repo, approved_head=sha, allow_dirty_tree=False
    )

    assert independent.sha == hardlinked.sha == sha
    assert independent.filesystem_sha256 != hardlinked.filesystem_sha256


def test_source_identity_rejects_a_hard_link_outside_the_worktree(tmp_path):
    repo, sha = _repo(tmp_path, ("gnutrue",))
    os.link(repo / "workflow.toml", tmp_path / "outside-link")

    with pytest.raises(validation.ValidationHookError, match="hard links outside"):
        validation.source_identity(
            repo, approved_head=sha, allow_dirty_tree=False
        )


def test_validation_detects_a_new_hard_link_after_child_exit(tmp_path, monkeypatch):
    repo, _ = _repo(tmp_path, ("gnutrue",))
    first = repo / "first.txt"
    second = repo / "second.txt"
    first.write_text("same bytes\n", encoding="utf-8")
    second.write_text("same bytes\n", encoding="utf-8")
    _git(repo, "add", "first.txt", "second.txt")
    _git(repo, "commit", "-qm", "add equal files")
    sha = _git(repo, "rev-parse", "HEAD")
    signer = _signer()
    source = validation.source_identity(
        repo, approved_head=sha, allow_dirty_tree=False
    )
    artifact = tmp_path / "result.json"
    _artifact(
        artifact,
        signer,
        task="task-1",
        base=sha,
        head=sha,
        hook="acceptance",
        commands=_actual(repo, ("gnutrue",)),
        source_filesystem_sha256=source.filesystem_sha256,
    )

    def replace_with_hard_link(argv, **kwargs):
        second.unlink()
        os.link(first, second)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(validation, "run_validation_child", replace_with_hard_link)
    with pytest.raises(validation.UnboundResultError, match="source changed"):
        validation.run_hook(
            worktree=repo,
            hook="acceptance",
            base_sha=sha,
            head_sha=sha,
            task_id="task-1",
            result_artifact=artifact,
            coordinator_public_key=signer.public_key,
            extra=[],
        )


def test_validation_config_snapshot_retains_only_kernel_allowed_keys(tmp_path):
    repo, sha = _repo(tmp_path, ("gnutrue",))
    _git(repo, "config", "--local", "user.name", "Candidate")
    _git(repo, "config", "--local", "remote.backup.url", "file:///srv/repo")
    _git(repo, "config", "--local", "remote.backup.fetch", "+refs/*:refs/*")
    _git(repo, "config", "--local", "branch.main.remote", "backup")
    _git(repo, "config", "--local", "branch.main.merge", "refs/heads/main")

    overlays = validation._repository_config_overlays(
        validation._repository_layout(repo)
    )
    assert len(overlays) == 1
    entries = validation.validated_git_config_entries(overlays[0].payload)
    assert entries == (
        ("core.repositoryformatversion", "0"),
        ("core.bare", "false"),
    )


def test_validation_config_screening_accepts_a_cloned_repository_with_origin(tmp_path):
    upstream, sha = _repo(tmp_path, ("gnutrue",))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "remote", "add", "backup", "https://example.invalid/backup.git")
    config = (clone / ".git" / "config").read_bytes()

    from loopzero.kernel import git_config_security

    assert git_config_security.origin_url(config) == str(upstream)
    overlays = validation._repository_config_overlays(
        validation._repository_layout(clone)
    )
    assert len(overlays) == 1
    assert validation.validated_git_config_entries(overlays[0].payload) == (
        ("core.repositoryformatversion", "0"),
        ("core.bare", "false"),
    )
    assert validation.source_identity(clone, approved_head=sha, allow_dirty_tree=False)
    _git(clone, "config", "--local", "remote.origin.url", "ext::sh -c owned")
    with pytest.raises(validation.ValidationHookError, match="unsafe"):
        validation._repository_config_overlays(validation._repository_layout(clone))


def test_source_identity_hashes_ignored_files(tmp_path):
    repo, sha = _repo(tmp_path, ("gnutrue",))
    (repo / ".gitignore").write_text("ignored-input\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore candidate input")
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "ignored-input").write_text("candidate-controlled\n", encoding="utf-8")

    with pytest.raises(validation.ValidationHookError, match="allow-dirty-tree"):
        validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)
    source = validation.source_identity(repo, approved_head=sha, allow_dirty_tree=True)
    assert source.kind == "tree"
    assert source.sha != sha


def test_final_ci_repro_task_refuses_noncanonical_command_authority(tmp_path):
    repo, sha = _repo(tmp_path, ("/tmp/attacker",))
    with pytest.raises(validation.ValidationHookError, match="shared allowlist"):
        validation._command_argv(repo, "/tmp/attacker", [])


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_signed_final_ci_repro_runs_provider_sandbox_without_outer_user_namespace(tmp_path):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    result = _run(repo, sha, artifact, key, env=_environment(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_hermetic_hook_can_write_private_cache_and_pass(tmp_path):
    repo, sha, signer, key, artifact = _script_case(
        tmp_path,
        'test "$PYTHONDONTWRITEBYTECODE" = 1\n'
        'case "$PYTEST_ADDOPTS" in *cache_dir=/tmp/*) ;; *) exit 8 ;; esac\n'
        'case "$UV_CACHE_DIR" in /tmp/*) ;; *) exit 9 ;; esac\n'
        'case "$npm_config_cache" in /tmp/*) ;; *) exit 10 ;; esac\n'
        'case "$UV_PROJECT_ENVIRONMENT" in /tmp/*) ;; *) exit 11 ;; esac\n'
        'test "$UV_NO_SYNC" = 1 || exit 12\n'
        'mkdir -p "$UV_PROJECT_ENVIRONMENT" && test ! -e .venv || exit 13\n'
        'mkdir -p "$UV_CACHE_DIR" "$npm_config_cache"\n'
        'printf cache > "$UV_CACHE_DIR/hook-cache"\n'
        'printf cache > "$npm_config_cache/hook-cache"',
    )

    result = _run(repo, sha, artifact, key, env=_environment(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_hermetic_hook_tracked_write_is_rejected_with_binding_exit(tmp_path):
    repo, sha, signer, key, artifact = _script_case(
        tmp_path,
        "printf 'changed\\n' > workflow.toml",
    )

    result = _run(repo, sha, artifact, key, env=_environment(tmp_path))

    assert result.returncode == 127, result.stdout + result.stderr
    assert "source changed during hook execution" in result.stderr


def test_namespace_probe_failure_is_operational_not_a_hook_exit(
    tmp_path, monkeypatch, capsys
):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    monkeypatch.setattr(
        capabilities,
        "probe_bwrap",
        lambda _bwrap: subprocess.CompletedProcess(
            ["bwrap"], 1, "", "bwrap: Creating new namespace failed"
        ),
    )

    code = validation.main(
        [
            "--worktree",
            str(repo),
            "--base",
            sha,
            "--head",
            sha,
            "--task-id",
            "task-1",
            "--result-artifact",
            str(artifact),
            "--coordinator-public-key",
            str(key),
            "--hook",
            "acceptance",
        ]
    )

    assert code == 2
    diagnostic = capsys.readouterr().err
    assert "validation hook unavailable" in diagnostic
    assert "sandbox namespace is unavailable" in diagnostic
    assert "Creating new namespace failed" in diagnostic


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
@pytest.mark.parametrize(
    ("failure", "expected_code", "diagnostic"),
    [
        ("unsigned", 125, "unsigned"),
        ("malformed", 125, "unsigned"),
        ("tampered", 126, "signature"),
        ("unbound", 127, "binding"),
    ],
)
def test_validation_result_rejection_taxonomy_through_job_main(
    tmp_path, failure, expected_code, diagnostic
):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    if failure == "unsigned":
        artifact.write_text('{"status":"completed"}', encoding="utf-8")
    elif failure == "malformed":
        artifact.write_text("{", encoding="utf-8")
    elif failure == "tampered":
        row = json.loads(artifact.read_text(encoding="utf-8"))
        row["status"] = "failed"
        artifact.write_text(json.dumps(row), encoding="utf-8")
    else:
        _artifact(
            artifact, signer, task="task-1", base=sha, head=sha,
            hook="acceptance", commands=_actual(repo, ("gnutrue",)),
            source_sha="f" * 40,
        )

    result = _run(repo, sha, artifact, key, env=_environment(tmp_path))

    assert result.returncode == expected_code, result.stdout + result.stderr
    assert f"rejected ({diagnostic})" in result.stderr


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
@pytest.mark.parametrize("artifact_kind", ["missing", "fifo", "symlink"])
def test_validation_unsafe_or_missing_result_is_bounded_unsigned_rejection(
    tmp_path, artifact_kind
):
    repo, sha, signer, key, artifact = _signed_case(tmp_path)
    artifact.unlink()
    if artifact_kind == "fifo":
        os.mkfifo(artifact)
    elif artifact_kind == "symlink":
        target = tmp_path / "artifact-target"
        target.write_text("{}", encoding="utf-8")
        artifact.symlink_to(target)

    started = time.monotonic()
    result = _run(
        repo, sha, artifact, key, env=_environment(tmp_path), timeout=8
    )

    assert time.monotonic() - started < 8
    assert result.returncode == 125, result.stdout + result.stderr
    assert "rejected (unsigned)" in result.stderr


def test_result_read_deadline_covers_a_slow_growing_regular_file(
    tmp_path, monkeypatch
):
    artifact = tmp_path / "slow-result"
    artifact.write_bytes(b"{" + b" " * 128)
    real_read = os.read
    stopped = threading.Event()

    def grow() -> None:
        while not stopped.wait(0.005):
            with artifact.open("ab") as handle:
                handle.write(b" ")

    def slow_read(descriptor: int, maximum: int) -> bytes:
        time.sleep(0.02)
        return real_read(descriptor, min(maximum, 1))

    writer = threading.Thread(target=grow, daemon=True)
    writer.start()
    monkeypatch.setattr(validation.os, "read", slow_read)
    started = time.monotonic()
    try:
        with pytest.raises(validation.UnsignedResultError, match="timed out"):
            validation._read_regular(
                artifact,
                maximum=validation.MAX_RESULT_BYTES,
                label="validation result artifact",
                error_type=validation.UnsignedResultError,
                timeout=0.05,
            )
    finally:
        stopped.set()
        writer.join(timeout=1)
    assert time.monotonic() - started < 0.5


def test_result_read_deadline_rejects_a_fifo_without_blocking(tmp_path):
    artifact = tmp_path / "result-fifo"
    os.mkfifo(artifact)
    started = time.monotonic()
    with pytest.raises(validation.UnsignedResultError, match="bounded regular file"):
        validation._read_regular(
            artifact,
            maximum=validation.MAX_RESULT_BYTES,
            label="validation result artifact",
            error_type=validation.UnsignedResultError,
            timeout=0.05,
        )
    assert time.monotonic() - started < 0.5


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_signed_final_ci_repro_reaps_delayed_descendants(tmp_path):
    repo, sha = _repo(tmp_path, ("./orphan",))
    marker = repo / "escaped"
    (repo / "orphan").write_text("#!/bin/sh\n(sleep 1; touch escaped) &\nsleep 30\n", encoding="utf-8")
    (repo / "orphan").chmod(0o755); _git(repo, "add", "."); _git(repo, "commit", "-qm", "script")
    sha = _git(repo, "rev-parse", "HEAD")
    signer = _signer(); key = tmp_path / "key"; key.write_bytes(signer.public_key)
    source = validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)
    artifact = tmp_path / "result"; _artifact(artifact, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, ("./orphan",)),
        source_filesystem_sha256=source.filesystem_sha256)
    result = _run(repo, sha, artifact, key, env=_environment(tmp_path, 0.2))
    assert result.returncode == 124
    time.sleep(1.2); assert not marker.exists()


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_signed_final_ci_repro_blocks_user_manager_escape(tmp_path):
    repo, sha = _repo(tmp_path, ("./escape",))
    (repo / "escape").write_text("#!/bin/sh\n! /usr/bin/systemctl --user start forged.service >/dev/null 2>&1\n", encoding="utf-8")
    (repo / "escape").chmod(0o755); _git(repo, "add", "."); _git(repo, "commit", "-qm", "escape")
    sha = _git(repo, "rev-parse", "HEAD"); signer = _signer(); key = tmp_path / "key"; key.write_bytes(signer.public_key)
    source = validation.source_identity(repo, approved_head=sha, allow_dirty_tree=False)
    artifact = tmp_path / "result"; _artifact(artifact, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, ("./escape",)),
        source_filesystem_sha256=source.filesystem_sha256)
    assert _run(repo, sha, artifact, key, env=_environment(tmp_path)).returncode == 0


def test_preflight_completes_when_collectors_respond(tmp_path, monkeypatch):
    repo, sha, signer, key, artifact = _signed_case(tmp_path, ("gnutrue", "gnutrue"))
    calls = []
    def real(argv, **kwargs):
        calls.append(argv)
        return _direct_child(argv, **kwargs)
    monkeypatch.setattr(validation, "run_validation_child", real)
    assert validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
        task_id="task-1", result_artifact=artifact, coordinator_public_key=signer.public_key, extra=[]) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("code", [1, 3])
def test_preflight_preserves_collector_failure_codes(tmp_path, monkeypatch, code):
    repo, sha, signer, key, artifact = _script_case(tmp_path, f"echo collector exit {code} >&2\nexit {code}")
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    assert validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
        task_id="task-1", result_artifact=artifact, coordinator_public_key=signer.public_key, extra=[]) == code


def test_preflight_auth_failure_requests_one_intake_login(tmp_path, monkeypatch, capsys):
    repo, sha, signer, key, artifact = _script_case(
        tmp_path, "echo 'authentication failed; login once' >&2\nexit 1", name="auth"
    )
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    assert validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
        task_id="task-1", result_artifact=artifact, coordinator_public_key=signer.public_key, extra=[]) == 1
    assert capsys.readouterr().err.count("login once") == 1


def test_preflight_fails_before_execution_when_tool_unavailable(tmp_path):
    repo, sha = _repo(tmp_path, ("missing-loopzero-tool",))
    with pytest.raises(validation.ValidationHookError, match="shared allowlist"):
        validation._command_argv(repo, "missing-loopzero-tool", [])


def test_preflight_checks_opus_auth_once_at_intake(tmp_path, monkeypatch):
    marker = tmp_path / "auth-calls"
    repo, sha, signer, key, artifact = _script_case(
        tmp_path, f"echo call >> {shlex.quote(str(marker))}", name="auth"
    )
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
        task_id="task-1", result_artifact=artifact, coordinator_public_key=signer.public_key, extra=[])
    assert marker.read_text(encoding="utf-8").splitlines() == ["call"]


def test_preflight_provider_timeout_does_not_claim_login_guidance_was_reported(
    tmp_path, monkeypatch
):
    repo, sha, signer, key, artifact = _script_case(tmp_path, "sleep 5", name="provider")
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    monkeypatch.setattr(validation, "settings", KernelSettings(toolchain={"validation_timeout_s": 0.05}))
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[])
    assert "login" not in str(failure.value).lower()


def test_preflight_stalled_lanes_collector_exits_within_bound_and_names_it(
    tmp_path, monkeypatch
):
    repo, sha, signer, key, artifact = _script_case(tmp_path, "sleep 5", name="lanes-collector")
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    monkeypatch.setattr(validation, "settings", KernelSettings(toolchain={"validation_timeout_s": 0.05}))
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[])
    assert time.monotonic() - started < 1
    assert "lanes-collector" in str(failure.value.cmd)


def test_preflight_preserves_collision_guard_exit_three(tmp_path, monkeypatch):
    repo, sha, signer, key, artifact = _script_case(tmp_path, "exit 3", name="collision")
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    assert validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
        task_id="task-1", result_artifact=artifact,
        coordinator_public_key=signer.public_key, extra=[]) == 3


def test_preflight_collision_timeout_is_failure_not_collision(tmp_path, monkeypatch):
    repo, sha, signer, key, artifact = _script_case(tmp_path, "sleep 5", name="collision")
    monkeypatch.setattr(validation, "run_validation_child", _direct_child)
    monkeypatch.setattr(validation, "settings", KernelSettings(toolchain={"validation_timeout_s": 0.05}))
    with pytest.raises(subprocess.TimeoutExpired):
        validation.run_hook(worktree=repo, hook="acceptance", base_sha=sha, head_sha=sha,
            task_id="task-1", result_artifact=artifact,
            coordinator_public_key=signer.public_key, extra=[])
