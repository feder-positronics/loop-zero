"""Behavioral security specifications for host-authenticated validation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loopzero.kernel import authority, validation
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
              base: str, head: str, hook: str, commands: list[str], **changes) -> None:
    row = {
        "schema_version": validation.RESULT_SCHEMA, "task_id": task,
        "base_sha": base, "head_sha": head, "hook": hook,
        "hook_commands": commands, "status": "completed", "exit_code": 0,
    }
    row.update(changes)
    path.write_text(json.dumps(signer.seal(row, authority_kind="coordinator")), encoding="utf-8")


def _environment(tmp_path: Path, timeout: float = 5) -> dict[str, str]:
    settings = KernelSettings(env_prefix="INTELFLO", state_root=tmp_path / "state",
                              toolchain={"validation_timeout_s": timeout})
    return package_environment({"PATH": "/usr/bin:/bin", **settings.child_environment()})


def _run(repo: Path, base: str, artifact: Path, key: Path, *, task: str = "task-1",
         hook: str = "acceptance", extra: tuple[str, ...] = (), timeout: float = 10,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(JOB_SH), "consumer-hook", "--base", base, "--head", base,
         "--task-id", task, "--result-artifact", str(artifact),
         "--coordinator-public-key", str(key), "--hook", hook, "--", *extra],
        cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
    )


def _signed_case(tmp_path: Path, commands: tuple[str, ...] = ("gnutrue",)):
    repo, sha = _repo(tmp_path, commands)
    signer = _signer()
    key = tmp_path / "coordinator.der"
    key.write_bytes(signer.public_key)
    result = tmp_path / "result.json"
    _artifact(result, signer, task="task-1", base=sha, head=sha,
              hook="acceptance", commands=_actual(repo, commands))
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
    _artifact(artifact, signer, task="task-1", base=sha, head=sha,
              hook="acceptance", commands=_actual(repo, commands))
    return repo, sha, signer, key, artifact


def _direct_child(argv, *, worktree, timeout, **kwargs):
    return subprocess.run(
        argv, cwd=worktree, capture_output=True, text=True,
        timeout=timeout, start_new_session=True,
    )


def test_signed_result_accepts_real_coordinator_signature(tmp_path):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    assert validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
        task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance",
        hook_commands=_actual(repo, ("gnutrue",)))["status"] == "completed"


def test_final_ci_repro_refuses_an_unsigned_terminal_artifact(tmp_path):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    result.write_text('{"status":"completed"}', encoding="utf-8")
    with pytest.raises(validation.UnsignedResultError):
        validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)))


def test_signed_result_rejects_real_tampering(tmp_path):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    row = json.loads(result.read_text()); row["status"] = "failed"
    result.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(validation.TamperedResultError):
        validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)))


@pytest.mark.parametrize("field,value", [("task_id", "other"), ("base_sha", "f" * 40),
                                          ("head_sha", "e" * 40), ("hook_commands", ["false"])])
def test_signed_result_rejects_unbound_fields(tmp_path, field, value):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    _artifact(result, signer, task="task-1", base=sha, head=sha, hook="acceptance",
              commands=_actual(repo, ("gnutrue",)), **{field: value})
    with pytest.raises(validation.UnboundResultError, match=field):
        validation.verify_result_artifact(result, coordinator_public_key=signer.public_key,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)))


def test_signed_final_ci_repro_rejects_authority_path_substitution(tmp_path):
    repo, sha, trusted, key, result = _signed_case(tmp_path)
    pinned = validation.read_coordinator_public_key(key)
    attacker = _signer(); key.write_bytes(attacker.public_key)
    _artifact(result, attacker, task="task-1", base=sha, head=sha, hook="acceptance", commands=_actual(repo, ("gnutrue",)))
    with pytest.raises(validation.TamperedResultError):
        validation.verify_result_artifact(result, coordinator_public_key=pinned,
            task_id="task-1", base_sha=sha, head_sha=sha, hook="acceptance", hook_commands=_actual(repo, ("gnutrue",)))


def test_explicit_base_ignores_ambient_serialized_redirect(tmp_path, monkeypatch):
    repo, sha, signer, key, result = _signed_case(tmp_path)
    monkeypatch.setenv("LOOPZERO_KERNEL_SETTINGS", json.dumps({"toolchain": {"approved_base": "refs/heads/attacker"}}))
    assert validation.resolve_base(repo, base=sha) == sha


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
def test_signed_final_ci_repro_reaps_delayed_descendants(tmp_path):
    repo, sha = _repo(tmp_path, ("./orphan",))
    marker = repo / "escaped"
    (repo / "orphan").write_text("#!/bin/sh\n(sleep 1; touch escaped) &\nsleep 30\n", encoding="utf-8")
    (repo / "orphan").chmod(0o755); _git(repo, "add", "."); _git(repo, "commit", "-qm", "script")
    sha = _git(repo, "rev-parse", "HEAD")
    signer = _signer(); key = tmp_path / "key"; key.write_bytes(signer.public_key)
    artifact = tmp_path / "result"; _artifact(artifact, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, ("./orphan",)))
    result = _run(repo, sha, artifact, key, env=_environment(tmp_path, 0.2))
    assert result.returncode == 124
    time.sleep(1.2); assert not marker.exists()


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_signed_final_ci_repro_blocks_user_manager_escape(tmp_path):
    repo, sha = _repo(tmp_path, ("./escape",))
    (repo / "escape").write_text("#!/bin/sh\n! /usr/bin/systemctl --user start forged.service >/dev/null 2>&1\n", encoding="utf-8")
    (repo / "escape").chmod(0o755); _git(repo, "add", "."); _git(repo, "commit", "-qm", "escape")
    sha = _git(repo, "rev-parse", "HEAD"); signer = _signer(); key = tmp_path / "key"; key.write_bytes(signer.public_key)
    artifact = tmp_path / "result"; _artifact(artifact, signer, task="task-1", base=sha, head=sha,
        hook="acceptance", commands=_actual(repo, ("./escape",)))
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
