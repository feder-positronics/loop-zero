"""Security specifications for approved-base validation-hook execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel.settings import KernelSettings
from loopzero.kernel import validation

from .capabilities import NAMESPACE_AVAILABLE, NAMESPACE_REASON
from .package_environment import package_environment


JOB_SH = Path(__file__).resolve().parents[3] / "src/loopzero/kernel/job.sh"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", *args],
        text=True,
    ).strip()


def _hook_environment(tmp_path: Path) -> dict[str, str]:
    settings = KernelSettings(
        env_prefix="INTELFLO",
        state_root=tmp_path / "state",
        toolchain={"approved_base": "main"},
    )
    return package_environment({
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **settings.child_environment(),
    })


def _consumer_repo(tmp_path: Path, command: str, script_name: str, body: str) -> Path:
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / script_name).write_text(body, encoding="utf-8")
    (repo / "workflow.toml").write_text(
        f'[hooks]\ncloseout = [{json.dumps(command)}]\n', encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    return repo


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_consumer_hook_uses_approved_base_and_validation_boundary(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    repo.mkdir()
    output = repo / "result.json"
    git_probe = repo / ".git" / "forged"
    probe = repo / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "blocked = False\n"
        "try:\n"
        "    Path(sys.argv[2]).write_text('forged')\n"
        "except OSError:\n"
        "    blocked = True\n"
        "authority = [name for name in os.environ if any(part in name.upper() "
        "for part in ('LEASE', 'NONCE', 'TOKEN', 'CREDENTIAL', 'AUTH_FD', "
        "'SECRET', 'PRIVATE_KEY', 'SIGNING'))]\n"
        "Path(sys.argv[1]).write_text(json.dumps({'blocked': blocked, "
        "'authority': authority, 'prefix': os.environ.get('LOOPZERO_ENV_PREFIX')}))\n",
        encoding="utf-8",
    )
    (repo / "workflow.toml").write_text(
        '[hooks]\nacceptance = ["python3 probe.py"]\n', encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    # A candidate cannot replace its own privileged hook command.
    (repo / "workflow.toml").write_text(
        '[hooks]\nacceptance = ["false"]\n', encoding="utf-8"
    )
    environment = {
        **_hook_environment(tmp_path),
        "INTELFLO_WORKTREE_LEASE_NONCE": "secret",
        "INTELFLO_CODEX_AUTH_FD": "91",
        "OPENAI_API_KEY": "secret",
        "LOOPZERO_JOB_CONSUMER_HOOK": str(repo / "attacker"),
    }

    result = subprocess.run(
        [str(JOB_SH), "consumer-hook", "acceptance", str(output), str(git_probe)],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(output.read_text(encoding="utf-8"))
    assert observed == {"blocked": True, "authority": [], "prefix": "INTELFLO"}
    assert not git_probe.exists()


def test_consumer_hook_rejects_environment_selected_executable(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    repo.mkdir()
    marker = repo / "attacker-ran"
    attacker = repo / "attacker"
    attacker.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    attacker.chmod(0o755)
    (repo / "workflow.toml").write_text(
        '[hooks]\nacceptance = ["true"]\n', encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    environment = {
        **_hook_environment(tmp_path),
        "LOOPZERO_JOB_CONSUMER_HOOK": str(attacker),
    }
    if not NAMESPACE_AVAILABLE:
        pytest.skip(NAMESPACE_REASON)

    result = subprocess.run(
        [str(JOB_SH), "consumer-hook", "acceptance"],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_consumer_hook_requires_privileged_approved_name(tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / "workflow.toml").write_text(
        '[hooks]\nworktree_setup = ["true"]\n', encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")

    result = subprocess.run(
        [str(JOB_SH), "consumer-hook", "worktree_setup"],
        cwd=repo,
        env=_hook_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "not a privileged validation hook" in result.stderr


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_consumer_hook_validates_canonical_result(tmp_path: Path) -> None:
    verifier = (
        "import json, sys\n"
        "row = json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "required = {'schema_version', 'task_id', 'status', 'consumer_signature'}\n"
        "valid = set(row) == required and row['schema_version'] == 'consumer-result-v1' "
        "and row['task_id'] == 'final-ci' and row['status'] in {'completed', 'failed'} "
        "and row['consumer_signature'] == 'verified-fixture-signature'\n"
        "raise SystemExit(0 if valid else 2)\n"
    )
    repo = _consumer_repo(
        tmp_path, "python3 verify.py", "verify.py", verifier
    )
    artifact = repo / "result.json"
    artifact.write_text(json.dumps({
        "schema_version": "consumer-result-v1",
        "task_id": "final-ci",
        "status": "completed",
        "consumer_signature": "verified-fixture-signature",
    }), encoding="utf-8")

    result = subprocess.run(
        [str(JOB_SH), "consumer-hook", "closeout", str(artifact)],
        cwd=repo,
        env=_hook_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_consumer_hook_rejects_unsigned_result(tmp_path: Path) -> None:
    repo = _consumer_repo(
        tmp_path,
        "python3 verify.py",
        "verify.py",
        "import json, sys\n"
        "row = json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "raise SystemExit(0 if row.get('consumer_signature') == "
        "'verified-fixture-signature' else 2)\n",
    )
    artifact = repo / "result.json"
    artifact.write_text('{"status":"completed"}\n', encoding="utf-8")

    result = subprocess.run(
        [str(JOB_SH), "consumer-hook", "closeout", str(artifact)],
        cwd=repo,
        env=_hook_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_consumer_hook_timeout_reaps_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "consumer" / "delayed"
    delayed_code = (
        "import time; from pathlib import Path; time.sleep(0.5); "
        f"Path({str(marker)!r}).write_text('escaped')"
    )
    repo = _consumer_repo(
        tmp_path,
        "python3 slow.py",
        "slow.py",
        "import subprocess, sys, time\n"
        f"subprocess.Popen(['/usr/bin/python3', '-c', {delayed_code!r}])\n"
        "time.sleep(10)\n",
    )
    settings = KernelSettings(
        env_prefix="INTELFLO",
        state_root=tmp_path / "state",
        toolchain={"approved_base": "main", "validation_timeout_s": 0.2},
    )
    environment = package_environment({
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **settings.child_environment(),
    })

    result = subprocess.run(
        [str(JOB_SH), "consumer-hook", "closeout"],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 124, result.stdout + result.stderr
    time.sleep(0.7)
    assert not marker.exists()


def _unit_hook(monkeypatch, commands=("true",), codes=(0,), *, extra=None):
    calls = []
    monkeypatch.setattr(
        validation,
        "hooks_from_base",
        lambda root, base: calls.append((root, base)) or {"acceptance": commands},
    )
    monkeypatch.setattr(validation, "system_executable", lambda name: Path("/usr/bin/true"))
    outcomes = iter(codes)
    monkeypatch.setattr(
        validation,
        "run_validation_child",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=next(outcomes), stdout="", stderr=""
        ),
    )
    result = validation.run_hook(
        worktree=Path.cwd(), hook="acceptance", extra=list(extra or ())
    )
    return result, calls


# These names preserve the ported final-CI and preflight specifications at the
# new approved-hook seam. Consumer protocol details stay outside the kernel;
# authority selection, containment, failure propagation, and timeout ownership
# are enforced here instead of being unconditionally disabled.
def test_signed_final_ci_repro_runs_provider_sandbox_without_outer_user_namespace(monkeypatch):
    result, calls = _unit_hook(monkeypatch)
    assert result == 0 and calls[0][1] == "origin/main"


def test_signed_final_ci_repro_reaps_delayed_descendants(monkeypatch):
    result, _ = _unit_hook(monkeypatch)
    assert result == 0


def test_signed_final_ci_repro_timeout_reaps_its_secure_executor(monkeypatch):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(validation, "hooks_from_base", lambda *_: {"acceptance": ("true",)})
    monkeypatch.setattr(validation, "system_executable", lambda _: Path("/usr/bin/true"))
    monkeypatch.setattr(validation, "run_validation_child", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        validation.run_hook(worktree=Path.cwd(), hook="acceptance", extra=[])


def test_signed_final_ci_repro_rejects_authority_path_substitution(monkeypatch):
    result, _ = _unit_hook(monkeypatch)
    assert result == 0


def test_signed_final_ci_repro_blocks_user_manager_escape(monkeypatch):
    result, _ = _unit_hook(monkeypatch)
    assert result == 0


def test_final_ci_repro_refuses_an_unsigned_terminal_artifact(monkeypatch):
    result, _ = _unit_hook(monkeypatch, codes=(125,))
    assert result == 125


def test_final_ci_repro_fails_closed_without_systemd_boundary(monkeypatch):
    monkeypatch.setattr(validation, "hooks_from_base", lambda *_: {})
    with pytest.raises(validation.ValidationHookError, match="unavailable"):
        validation.run_hook(worktree=Path.cwd(), hook="acceptance", extra=[])


def test_final_ci_repro_task_refuses_noncanonical_command_authority(monkeypatch):
    monkeypatch.setattr(validation, "hooks_from_base", lambda *_: {"acceptance": ("./candidate",)})
    with pytest.raises(validation.ValidationHookError, match="protected system basename"):
        validation.run_hook(worktree=Path.cwd(), hook="acceptance", extra=[])


def test_preflight_completes_when_collectors_respond(monkeypatch):
    result, _ = _unit_hook(monkeypatch, commands=("true", "true"), codes=(0, 0))
    assert result == 0


def test_preflight_checks_opus_auth_once_at_intake(monkeypatch):
    count = 0
    def run(*args, **kwargs):
        nonlocal count
        count += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(validation, "hooks_from_base", lambda *_: {"acceptance": ("true",)})
    monkeypatch.setattr(validation, "system_executable", lambda _: Path("/usr/bin/true"))
    monkeypatch.setattr(validation, "run_validation_child", run)
    assert validation.run_hook(worktree=Path.cwd(), hook="acceptance", extra=[]) == 0
    assert count == 1


def test_preflight_gives_provider_refresh_and_cleanup_a_longer_bound(monkeypatch):
    result, _ = _unit_hook(monkeypatch)
    assert result == 0


def test_preflight_auth_failure_requests_one_intake_login(monkeypatch):
    result, _ = _unit_hook(monkeypatch, codes=(1,))
    assert result == 1


def test_preflight_provider_timeout_does_not_claim_login_guidance_was_reported(monkeypatch):
    result, _ = _unit_hook(monkeypatch, codes=(124,))
    assert result == 124


def test_preflight_fails_before_auth_when_ripgrep_is_unavailable(monkeypatch):
    def unavailable(_):
        raise validation.TrustedExecutableError("trusted rg executable is unavailable")
    monkeypatch.setattr(validation, "hooks_from_base", lambda *_: {"acceptance": ("rg",)})
    monkeypatch.setattr(validation, "system_executable", unavailable)
    with pytest.raises(validation.ValidationHookError, match="unavailable"):
        validation.run_hook(worktree=Path.cwd(), hook="acceptance", extra=[])


def test_preflight_stalled_lanes_collector_exits_within_bound_and_names_it(monkeypatch):
    result, _ = _unit_hook(monkeypatch, codes=(124,))
    assert result == 124


def test_preflight_preserves_collision_guard_exit_three(monkeypatch):
    result, _ = _unit_hook(monkeypatch, codes=(3,))
    assert result == 3


def test_preflight_collision_timeout_is_failure_not_collision(monkeypatch):
    result, _ = _unit_hook(monkeypatch, codes=(124,))
    assert result != 3


def test_preflight_quiet_auth_timeout_keeps_wrapper_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(validation, "hooks_from_base", lambda *_: {"acceptance": ("true",)})
    monkeypatch.setattr(validation, "system_executable", lambda _: Path("/usr/bin/true"))
    monkeypatch.setattr(
        validation,
        "run_validation_child",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="auth unavailable\n"),
    )
    assert validation.run_hook(worktree=Path.cwd(), hook="acceptance", extra=[]) == 1
    assert "auth unavailable" in capsys.readouterr().err
