"""Coverage for the detached long-running-command runner used by agents."""

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "util" / "job.sh"
TRUSTED_JOB_PYTHON = "/usr/bin/python3"
REPRO_CONNECT_GUARD_PATH = REPO_ROOT / "scripts" / "util" / "repro_connect_guard.py"
REPRO_CONNECT_GUARD_SPEC = importlib.util.spec_from_file_location(
    "job_runner_repro_connect_guard", REPRO_CONNECT_GUARD_PATH
)
assert REPRO_CONNECT_GUARD_SPEC is not None
assert REPRO_CONNECT_GUARD_SPEC.loader is not None
REPRO_CONNECT_GUARD = importlib.util.module_from_spec(REPRO_CONNECT_GUARD_SPEC)
REPRO_CONNECT_GUARD_SPEC.loader.exec_module(REPRO_CONNECT_GUARD)
WORKTREE_LEASE_ENVIRONMENT = (
    "INTELFLO_WORKTREE_LEASE_FD",
    "INTELFLO_WORKTREE_LEASE_BOUNDARY",
    "INTELFLO_WORKTREE_LEASE_OWNER_PID",
    "INTELFLO_WORKTREE_LEASE_NONCE",
)
requires_nested_user_namespace = pytest.mark.skipif(
    os.environ.get("INTELFLO_GUARDIAN_SANDBOX_BOUNDARY") is not None,
    reason="bound-job protection cannot create a nested user namespace",
)
REPRO_GUARD_UNAVAILABLE_ERRORS = (
    REPRO_CONNECT_GUARD.SECCOMP_NOTIFY_UNAVAILABLE_ERROR,
    REPRO_CONNECT_GUARD.MANAGER_SOCKET_AUTHORITY_UNAVAILABLE_ERROR,
)


def test_final_ci_repro_unavailability_signatures_match_guard() -> None:
    assert REPRO_GUARD_UNAVAILABLE_ERRORS == (
        REPRO_CONNECT_GUARD.SECCOMP_NOTIFY_UNAVAILABLE_ERROR,
        REPRO_CONNECT_GUARD.MANAGER_SOCKET_AUTHORITY_UNAVAILABLE_ERROR,
    )


@pytest.fixture(autouse=True)
def _isolate_job_runner_from_parent_worktree_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in WORKTREE_LEASE_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def _job(
    tmp_path: Path,
    *args: str,
    timeout: float = 60,
    script: Path = SCRIPT,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
    }
    environment.update(env_overrides or {})
    return subprocess.run(
        [str(script), *args],
        cwd=script.resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _default_job(
    script: Path, *args: str, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=script.resolve().parents[2],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_job_runner_disables_hostile_python_startup_paths(tmp_path: Path) -> None:
    version_command = [
        "/usr/bin/python3",
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    ]
    version = subprocess.run(
        version_command,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    user_base = tmp_path / "user-base"
    user_site = user_base / "lib" / f"python{version}" / "site-packages"
    user_site.mkdir(parents=True)
    probe_marker = tmp_path / "probe-user-site-executed"
    job_marker = tmp_path / "job-user-site-executed"
    python_path_probe_marker = tmp_path / "probe-python-path-executed"
    python_path_job_marker = tmp_path / "job-python-path-executed"
    startup_hook = (
        "import os,pathlib; "
        "pathlib.Path(os.environ['INTELFLO_TEST_USER_SITE_MARKER'])"
        ".write_text('owned', encoding='utf-8')\n"
    )
    (user_site / "candidate_startup.pth").write_text(
        startup_hook,
        encoding="utf-8",
    )
    python_path = tmp_path / "python-path"
    python_path.mkdir()
    (python_path / "hashlib.py").write_text(
        "import os,pathlib\n"
        "pathlib.Path(os.environ['INTELFLO_TEST_PYTHONPATH_MARKER'])"
        ".write_text('owned', encoding='utf-8')\n",
        encoding="utf-8",
    )
    hostile_environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(python_path),
        "PYTHONUSERBASE": str(user_base),
    }

    probe = subprocess.run(
        ["/usr/bin/python3", "-c", "import hashlib"],
        env={
            **hostile_environment,
            "INTELFLO_TEST_PYTHONPATH_MARKER": str(python_path_probe_marker),
            "INTELFLO_TEST_USER_SITE_MARKER": str(probe_marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe_marker.exists()
    assert python_path_probe_marker.exists()

    result = _job(
        tmp_path,
        "list",
        env_overrides={
            **hostile_environment,
            "INTELFLO_TEST_PYTHONPATH_MARKER": str(python_path_job_marker),
            "INTELFLO_TEST_USER_SITE_MARKER": str(job_marker),
        },
    )

    assert result.returncode == 0, result.stderr
    assert not job_marker.exists()
    assert not python_path_job_marker.exists()


def test_snapshot_runner_keys_authority_to_declared_delivery(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()

    result = subprocess.run(
        [str(SCRIPT), "list"],
        cwd=delivery,
        env={
            "PATH": "/usr/bin:/bin",
            "INTELFLO_DELIVERY_ROOT": str(delivery),
            "INTELFLO_JOB_DIR": "snapshot-jobs",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert (delivery / "snapshot-jobs").is_dir()
    assert not (REPO_ROOT / "snapshot-jobs").exists()


def test_snapshot_runner_starts_job_in_declared_delivery_authority(
    tmp_path: Path,
) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()

    result = subprocess.run(
        [str(SCRIPT), "run", "snapshot-closeout", "--timeout", "30", "--", "true"],
        cwd=delivery,
        env={
            "PATH": "/usr/bin:/bin",
            "INTELFLO_DELIVERY_ROOT": str(delivery),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "exit code 0" in result.stdout


def test_run_reports_success_and_log_tail(tmp_path: Path) -> None:
    result = _job(tmp_path, "run", "ok", "--timeout", "30", "--", "echo", "hello")

    assert result.returncode == 0
    assert "exit code 0" in result.stdout
    assert "hello" in result.stdout


def test_run_propagates_the_job_exit_code(tmp_path: Path) -> None:
    result = _job(
        tmp_path, "run", "boom", "--timeout", "30", "--", "sh", "-c", "exit 7"
    )

    assert result.returncode == 7
    assert "exit code 7" in result.stdout


def test_run_preserves_wrapped_python_script_directory_imports(tmp_path: Path) -> None:
    result = _job(
        tmp_path,
        "run",
        "generate-indexes",
        "--timeout",
        "30",
        "--",
        "/usr/bin/python3",
        str(REPO_ROOT / "scripts" / "docs" / "generate_indexes.py"),
        "--check",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_wait_times_out_without_killing_the_job(tmp_path: Path) -> None:
    started = _job(tmp_path, "start", "slow", "--", "sleep", "30")
    assert started.returncode == 0

    waited = _job(tmp_path, "wait", "slow", "--timeout", "2")
    assert waited.returncode == 124
    assert "still running" in waited.stderr

    # A timed-out wait must leave the work in flight; killing it would discard
    # minutes of progress and force a rerun.
    status = _job(tmp_path, "status", "slow")
    assert "RUNNING" in status.stdout

    _job(tmp_path, "clean", "--all")


def test_start_returns_before_the_job_finishes(tmp_path: Path) -> None:
    started = _job(tmp_path, "start", "detached", "--", "sleep", "20", timeout=10)

    assert started.returncode == 0
    assert "started job 'detached'" in started.stdout
    assert "RUNNING" in _job(tmp_path, "status", "detached").stdout

    _job(tmp_path, "clean", "--all")


@pytest.mark.parametrize("name", ["bad/name", "-lead", ".hidden", ""])
def test_rejects_unsafe_job_names(tmp_path: Path, name: str) -> None:
    result = _job(tmp_path, "start", name, "--", "true")

    assert result.returncode == 2
    assert "job name" in result.stderr


def test_refuses_to_clobber_a_running_job(tmp_path: Path) -> None:
    assert _job(tmp_path, "start", "busy", "--", "sleep", "30").returncode == 0

    second = _job(tmp_path, "start", "busy", "--", "true")

    assert second.returncode == 2
    assert "already running" in second.stderr

    _job(tmp_path, "clean", "--all")


def test_check_passes_when_no_jobs_exist(tmp_path: Path) -> None:
    assert _job(tmp_path, "check").returncode == 0


def test_check_fails_while_a_job_is_running(tmp_path: Path) -> None:
    assert _job(tmp_path, "start", "inflight", "--", "sleep", "30").returncode == 0

    result = _job(tmp_path, "check")

    assert result.returncode == 1
    assert "still RUNNING" in result.stderr

    _job(tmp_path, "clean", "--all")


def test_check_fails_for_a_finished_but_unreaped_job(tmp_path: Path) -> None:
    # `start` without a matching `wait` means the exit code was never read —
    # the failure mode job.sh exists to prevent.
    assert _job(tmp_path, "start", "orphan", "--", "sh", "-c", "exit 3").returncode == 0
    _wait_until_done(tmp_path, "orphan")

    result = _job(tmp_path, "check")

    assert result.returncode == 1
    assert "never waited on" in result.stderr


def test_check_passes_once_the_job_is_waited_on(tmp_path: Path) -> None:
    assert (
        _job(tmp_path, "run", "reaped", "--timeout", "30", "--", "true").returncode == 0
    )

    assert _job(tmp_path, "check").returncode == 0


def test_check_ignores_only_the_authenticated_current_wrapper(tmp_path: Path) -> None:
    result = _job(
        tmp_path,
        "run",
        "self-check",
        "--timeout",
        "30",
        "--",
        str(SCRIPT),
        "check",
    )

    assert result.returncode == 0


def test_wrapped_command_cannot_inherit_terminal_sealing_authority(
    tmp_path: Path,
) -> None:
    command = (
        'test -z "${INTELFLO_JOB_TOKEN:-}" && '
        'test ! -e "$INTELFLO_JOB_DIR/no-replay/token" && '
        'test ! -e "$INTELFLO_JOB_DIR/no-replay/executor.json"'
    )

    result = _job(
        tmp_path,
        "run",
        "no-replay",
        "--timeout",
        "30",
        "--",
        "sh",
        "-c",
        command,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "jobs" / "no-replay" / "executor.json").exists()


@requires_nested_user_namespace
def test_bound_wrapped_command_cannot_mutate_its_job_authority(tmp_path: Path) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    pid_path = tmp_path / "jobs" / "bound" / "pid"
    command = f'! sh -c \'printf "forged\\n" > "{pid_path}"\''

    result = _job(
        tmp_path,
        "run",
        "bound",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        "sh",
        "-c",
        command,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "jobs" / "bound" / "exit_code").read_text().strip() == "0"


@requires_nested_user_namespace
def test_bound_job_pins_bubblewrap_outside_inherited_path(tmp_path: Path) -> None:
    attacker = tmp_path / "attacker-bin"
    attacker.mkdir()
    marker = tmp_path / "fake-bwrap-ran"
    fake_bwrap = attacker / "bwrap"
    fake_bwrap.write_text(f"#!/bin/sh\ntouch {marker}\nexit 77\n", encoding="utf-8")
    fake_bwrap.chmod(0o755)
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )

    result = _job(
        tmp_path,
        "run",
        "pinned-bwrap",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
        env_overrides={"PATH": f"{attacker}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


@requires_nested_user_namespace
def test_bound_wrapped_command_cannot_substitute_an_authority_ancestor(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    moved = tmp_path.with_name(f"{tmp_path.name}-moved")
    job_dir = tmp_path / "jobs" / "bound"
    attack = (
        f'mv "{tmp_path}" "{moved}" && mkdir -p "{job_dir}" && '
        f'printf "forged\\n" > "{job_dir / "binding.json"}"'
    )

    result = _job(
        tmp_path,
        "run",
        "bound",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        "sh",
        "-c",
        f"! sh -c {json.dumps(attack)}",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not moved.exists()
    assert (job_dir / "exit_code").read_text().strip() == "0"


@requires_nested_user_namespace
def test_bound_dispatcher_reuses_its_own_worker_sandbox(tmp_path: Path) -> None:
    script = _isolated_job_script(tmp_path)
    dispatcher = script.with_name("agent_dispatch.py")
    dispatcher.write_text(
        """import subprocess
import sys

assert sys.argv[1] == "run"
raise SystemExit(
    subprocess.run(
        ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--", "true"],
        check=False,
    ).returncode
)
""",
        encoding="utf-8",
    )
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )

    result = _job(
        tmp_path,
        "run",
        "bound-dispatch",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        sys.executable,
        str(dispatcher),
        "run",
        script=script,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("repo_style", ("default", "separate", "equals"))
@requires_nested_user_namespace
def test_bound_host_dispatcher_reuses_its_own_worker_sandbox(
    tmp_path: Path,
    repo_style: str,
) -> None:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    host_dispatcher = script.with_name("agent_dispatch_host.py")
    host_dispatcher.write_text(
        """import os
import subprocess

assert "INTELFLO_CODEX_AUTH_FD" not in os.environ
raise SystemExit(
    subprocess.run(
        ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--", "true"],
        check=False,
    ).returncode
)
""",
        encoding="utf-8",
    )
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    dispatcher_arguments = ["run"]
    if repo_style == "separate":
        dispatcher_arguments = ["--repo", str(repo), "run"]
    elif repo_style == "equals":
        dispatcher_arguments = [f"--repo={repo}", "run"]

    result = _job(
        tmp_path,
        "run",
        "bound-host-dispatch",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        TRUSTED_JOB_PYTHON,
        str(host_dispatcher),
        *dispatcher_arguments,
        script=script,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "variant",
    (
        "untrusted-executable",
        "lookalike-path",
        "arbitrary-script",
        "other-subcommand",
        "missing-subcommand",
        "repo-other-subcommand",
        "repo-missing-path",
        "repo-option-as-path",
        "empty-repo-equals",
        "symlink-path",
        "internal-authority",
        "internal-artifact",
    ),
)
def test_bound_host_dispatcher_trust_predicate_fails_closed(
    tmp_path: Path,
    variant: str,
) -> None:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    host_dispatcher = script.with_name("agent_dispatch_host.py")
    host_dispatcher.write_text("raise SystemExit(0)\n", encoding="utf-8")
    arbitrary_script = script.with_name("arbitrary.py")
    arbitrary_script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    lookalike = repo / "other" / "agent_dispatch_host.py"
    lookalike.parent.mkdir()
    lookalike.write_text("raise SystemExit(0)\n", encoding="utf-8")
    symlink = script.with_name("host-dispatcher-link.py")
    symlink.symlink_to(host_dispatcher.name)
    artifact = tmp_path / "terminal.json"
    if variant == "internal-artifact":
        artifact = repo / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    command = [TRUSTED_JOB_PYTHON, str(host_dispatcher), "run"]
    if variant == "untrusted-executable":
        command[0] = "/bin/sh"
    elif variant == "lookalike-path":
        command[1] = str(lookalike)
    elif variant == "arbitrary-script":
        command[1] = str(arbitrary_script)
    elif variant == "other-subcommand":
        command[2] = "preflight"
    elif variant == "missing-subcommand":
        command.pop()
    elif variant == "repo-other-subcommand":
        command[2:] = ["--repo", str(repo), "preflight"]
    elif variant == "repo-missing-path":
        command[2:] = ["--repo", "run"]
    elif variant == "repo-option-as-path":
        command[2:] = ["--repo", "--worktree", "run"]
    elif variant == "empty-repo-equals":
        command[2:] = ["--repo=", "run"]
    elif variant == "symlink-path":
        command[1] = str(symlink)
    env_overrides = None
    if variant == "internal-authority":
        env_overrides = {"INTELFLO_JOB_DIR": str(repo / ".pid" / "jobs")}
    _deny_outer_bubblewrap(script)

    result = _job(
        tmp_path,
        "run",
        f"bound-host-{variant}",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        *command,
        script=script,
        env_overrides=env_overrides,
    )

    assert result.returncode != 0
    assert "bound job supervision requires protected bubblewrap" in result.stdout


def test_inherited_codex_descriptor_is_invalidated_without_host_reacquisition(
    tmp_path: Path,
) -> None:
    script = _isolated_job_script(tmp_path)
    host_dispatcher = script.with_name("agent_dispatch_host.py")
    host_dispatcher.write_text(
        """import os

auth_fd = int(os.environ["INTELFLO_CODEX_AUTH_FD"])
assert auth_fd == -1
try:
    os.fstat(auth_fd)
except OSError:
    pass
else:
    raise AssertionError("invalid descriptor sentinel unexpectedly became live")
""",
        encoding="utf-8",
    )
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    _deny_outer_bubblewrap(script)

    result = _job(
        tmp_path,
        "run",
        "bound-host-inherited-auth",
        "--timeout",
        "30",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        TRUSTED_JOB_PYTHON,
        str(host_dispatcher),
        "run",
        script=script,
        env_overrides={"INTELFLO_CODEX_AUTH_FD": "23"},
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_unbound_host_dispatcher_gets_no_terminal_binding(tmp_path: Path) -> None:
    script = _isolated_job_script(tmp_path)
    host_dispatcher = script.with_name("agent_dispatch_host.py")
    host_dispatcher.write_text("raise SystemExit(0)\n", encoding="utf-8")
    _deny_outer_bubblewrap(script)

    result = _job(
        tmp_path,
        "run",
        "unbound-host-dispatch",
        "--timeout",
        "30",
        "--",
        TRUSTED_JOB_PYTHON,
        str(host_dispatcher),
        "run",
        script=script,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    job_dir = tmp_path / "jobs" / "unbound-host-dispatch"
    assert not (job_dir / "binding.json").exists()
    assert not (job_dir / "terminal-envelope.json").exists()


def test_default_authority_is_external_and_legacy_recovery_is_explicit(
    tmp_path: Path,
) -> None:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]

    current = _default_job(script, "run", "external", "--timeout", "30", "--", "true")
    assert current.returncode == 0, current.stdout + current.stderr
    authority_root = Path(
        subprocess.check_output(
            ["python3", str(script.with_name("job_store.py")), "--worktree", str(repo)],
            cwd=repo,
            env={"PATH": "/usr/bin:/bin"},
            text=True,
        ).strip()
    )
    assert not authority_root.is_relative_to(repo)
    assert (authority_root / "external").is_dir()
    assert stat.S_IMODE(authority_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((authority_root / "external").stat().st_mode) == 0o700
    assert not (repo / ".pid" / "jobs" / "external").exists()

    legacy_root = repo / ".pid" / "jobs"
    legacy = subprocess.run(
        [str(script), "run", "legacy", "--timeout", "30", "--", "true"],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "INTELFLO_JOB_DIR": str(legacy_root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    hidden = _default_job(script, "status", "legacy")
    assert hidden.returncode == 2
    recovered = subprocess.run(
        [str(script), "status", "legacy"],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "INTELFLO_JOB_DIR": str(legacy_root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "DONE exit=0" in recovered.stdout
    assert _default_job(script, "clean", "--all").returncode == 0


@requires_nested_user_namespace
def test_bound_job_rejects_replayed_executor_with_a_different_source(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps({"task_id": "foreign", "status": "completed"}),
        encoding="utf-8",
    )
    job_dir = tmp_path / "jobs" / "bound"
    pid_path = job_dir / "pid"
    command = (
        f'original_pid=$(cat "{pid_path}"); '
        f'sh -c \'printf "%s\\n" "$$" > "{pid_path}"; '
        f'exec "{SCRIPT}" _execute "{job_dir}" "{forged}" -- true\' || true; '
        f'printf "%s\\n" "$original_pid" > "{pid_path}" || true'
    )

    assert (
        _job(
            tmp_path,
            "start",
            "bound",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "dispatch-closeout",
            "--terminal-artifact",
            str(artifact),
            "--",
            "sh",
            "-c",
            command,
        ).returncode
        == 0
    )
    _wait_until_done(tmp_path, "bound")

    assert (job_dir / "exit_code").read_text(encoding="utf-8").strip() == "0"
    envelope = json.loads(
        (job_dir / "terminal-envelope.json").read_text(encoding="utf-8")
    )
    assert envelope["task_id"] == "dispatch-closeout"
    assert envelope["terminal"]["task_id"] == "dispatch-closeout"
    assert not (job_dir / "executor.json").exists()


@requires_nested_user_namespace
def test_bound_job_terminal_files_are_create_once_against_executor_replay(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(
            {
                "task_id": "dispatch-closeout",
                "status": "completed",
                "forged": True,
            }
        ),
        encoding="utf-8",
    )
    assert (
        _job(
            tmp_path,
            "run",
            "bound",
            "--timeout",
            "30",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "dispatch-closeout",
            "--terminal-artifact",
            str(artifact),
            "--",
            "true",
        ).returncode
        == 0
    )
    job_dir = tmp_path / "jobs" / "bound"
    durable_names = (
        "terminal-envelope.json",
        "terminal-envelope.sha256",
        "exit_code",
    )
    originals = {name: (job_dir / name).read_bytes() for name in durable_names}
    lease = _lease_path(tmp_path, "bound")
    attack = (
        f'exec 9<>"{lease}"; flock -n 9; '
        f'printf "%s\\n" "$$" > "{job_dir / "pid"}"; '
        f'exec "{SCRIPT}" _execute "{job_dir}" "{forged}" 9 -- true'
    )

    replay = subprocess.run(
        ["sh", "-c", attack],
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
            "INTELFLO_JOB_NAME": "bound",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert replay.returncode == 125
    assert {name: (job_dir / name).read_bytes() for name in durable_names} == originals


def test_authenticated_self_check_still_rejects_a_running_sibling(
    tmp_path: Path,
) -> None:
    command = f"{SCRIPT} start sibling -- sleep 30 >/dev/null && {SCRIPT} check"
    try:
        result = _job(
            tmp_path,
            "run",
            "self-check",
            "--timeout",
            "30",
            "--",
            "sh",
            "-c",
            command,
        )

        assert result.returncode == 1
        assert "job 'sibling' is still RUNNING" in result.stdout
        assert "job 'self-check' is still RUNNING" not in result.stdout
    finally:
        _job(tmp_path, "clean", "--all")


def test_wait_on_unknown_job_is_a_usage_error(tmp_path: Path) -> None:
    result = _job(tmp_path, "wait", "ghost")

    assert result.returncode == 2
    assert "no such job" in result.stderr


def test_wait_cannot_reap_a_reused_job_generation(tmp_path: Path) -> None:
    assert _job(tmp_path, "run", "generation", "--", "true").returncode == 0
    ready = tmp_path / "wait-hook-ready"
    release = tmp_path / "wait-hook-release"
    script = _instrumented_job_script(
        tmp_path,
        marker=(
            '\t\twrite_waited_marker "$dir" '
            "|| die \"job '$name' waited marker is unsafe\""
        ),
        replacement=(
            '\t\t: >"${INTELFLO_WAIT_HOOK_READY:?}"\n'
            '\t\twhile [ ! -e "${INTELFLO_WAIT_HOOK_RELEASE:?}" ]; do sleep 0.05; done\n'
            '\t\twrite_waited_marker "$dir" '
            "|| die \"job '$name' waited marker is unsafe\""
        ),
    )
    waiter = subprocess.Popen(
        [str(script), "wait", "generation", "--timeout", "30"],
        cwd=script.resolve().parents[2],
        env={
            "PATH": "/usr/bin:/bin",
            "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
            "INTELFLO_WAIT_HOOK_READY": str(ready),
            "INTELFLO_WAIT_HOOK_RELEASE": str(release),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.02)
        else:
            pytest.fail("waiter did not reach the pre-reap hook")

        replacement = _job(tmp_path, "start", "generation", "--", "sleep", "30")

        assert replacement.returncode == 2
        assert "active launcher or supervisor" in replacement.stderr
        release.touch()
        _, waiter_stderr = waiter.communicate(timeout=5)
        assert waiter.returncode == 0, waiter_stderr
    finally:
        release.touch()
        if waiter.poll() is None:
            waiter.terminate()
            waiter.wait(timeout=5)
        _job(tmp_path, "clean", "--all")


def test_wait_rejects_observed_job_directory_generation_drift(tmp_path: Path) -> None:
    assert _job(tmp_path, "run", "generation-drift", "--", "true").returncode == 0
    job_dir = tmp_path / "jobs" / "generation-drift"
    (job_dir / "waited").unlink()
    original_generation = job_dir.stat()
    ready = tmp_path / "generation-drift-ready"
    release = tmp_path / "generation-drift-release"
    marker = (
        "\t# Local sleep loop: cheap (no model round-trip), unlike write_stdin polling."
    )
    script = _instrumented_job_script(
        tmp_path,
        marker=marker,
        replacement=(
            '\t: >"${INTELFLO_WAIT_HOOK_READY:?}"\n'
            '\twhile [ ! -e "${INTELFLO_WAIT_HOOK_RELEASE:?}" ]; do sleep 0.05; done\n'
            f"{marker}"
        ),
    )
    waiter = subprocess.Popen(
        [str(script), "wait", "generation-drift", "--timeout", "30"],
        cwd=script.resolve().parents[2],
        env={
            "PATH": "/usr/bin:/bin",
            "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
            "INTELFLO_WAIT_HOOK_READY": str(ready),
            "INTELFLO_WAIT_HOOK_RELEASE": str(release),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    old_job_dir = tmp_path / "jobs" / "generation-drift-old"
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.02)
        else:
            pytest.fail("waiter did not capture the original job generation")

        held_descriptor = None
        for descriptor in Path(f"/proc/{waiter.pid}/fd").iterdir():
            try:
                held = descriptor.stat()
            except OSError:
                continue
            if (held.st_dev, held.st_ino) == (
                original_generation.st_dev,
                original_generation.st_ino,
            ):
                held_descriptor = descriptor
                break
        assert held_descriptor is not None, "waiter did not pin the observed generation"

        job_dir.rename(old_job_dir)
        shutil.rmtree(old_job_dir)
        pinned = held_descriptor.stat()
        assert (pinned.st_dev, pinned.st_ino) == (
            original_generation.st_dev,
            original_generation.st_ino,
        )
        job_dir.mkdir(mode=0o700)
        release.touch()
        _, waiter_stderr = waiter.communicate(timeout=5)

        assert waiter.returncode == 2
        assert "generation changed before its result could be reaped" in waiter_stderr
        assert not (job_dir / "waited").exists()
    finally:
        release.touch()
        if waiter.poll() is None:
            waiter.terminate()
            waiter.wait(timeout=5)
        _job(tmp_path, "clean", "--all")
        shutil.rmtree(old_job_dir, ignore_errors=True)


def test_wait_keeps_using_pinned_directory_after_generation_verification(
    tmp_path: Path,
) -> None:
    assert _job(tmp_path, "run", "pinned-reap", "--", "true").returncode == 0
    job_dir = tmp_path / "jobs" / "pinned-reap"
    (job_dir / "waited").unlink()
    ready = tmp_path / "pinned-reap-ready"
    release = tmp_path / "pinned-reap-release"
    marker = '\tif [ -f "$dir/exit_code" ] && [ ! -L "$dir/exit_code" ]; then'
    script = _instrumented_job_script(
        tmp_path,
        marker=marker,
        replacement=(
            '\t: >"${INTELFLO_WAIT_HOOK_READY:?}"\n'
            '\twhile [ ! -e "${INTELFLO_WAIT_HOOK_RELEASE:?}" ]; do sleep 0.05; done\n'
            f"{marker}"
        ),
    )
    waiter = subprocess.Popen(
        [str(script), "wait", "pinned-reap", "--timeout", "30"],
        cwd=script.resolve().parents[2],
        env={
            "PATH": "/usr/bin:/bin",
            "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
            "INTELFLO_WAIT_HOOK_READY": str(ready),
            "INTELFLO_WAIT_HOOK_RELEASE": str(release),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    old_job_dir = tmp_path / "jobs" / "pinned-reap-old"
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.02)
        else:
            pytest.fail("waiter did not finish generation verification")

        job_dir.rename(old_job_dir)
        job_dir.mkdir(mode=0o700)
        (job_dir / "exit_code").write_text("7\n", encoding="ascii")
        (job_dir / "log").write_text("replacement\n", encoding="utf-8")
        release.touch()
        _, waiter_stderr = waiter.communicate(timeout=5)

        assert waiter.returncode == 0, waiter_stderr
        assert (old_job_dir / "waited").is_file()
        assert not (job_dir / "waited").exists()
    finally:
        release.touch()
        if waiter.poll() is None:
            waiter.terminate()
            waiter.wait(timeout=5)
        _job(tmp_path, "clean", "--all")
        shutil.rmtree(old_job_dir, ignore_errors=True)


def test_wait_rejects_a_symlinked_waited_marker(tmp_path: Path) -> None:
    assert _job(tmp_path, "run", "waited-symlink", "--", "true").returncode == 0
    job_dir = tmp_path / "jobs" / "waited-symlink"
    (job_dir / "waited").unlink()
    target = tmp_path / "waited-target"
    target.write_text("preserve\n", encoding="utf-8")
    (job_dir / "waited").symlink_to(target)

    result = _job(tmp_path, "wait", "waited-symlink")

    assert result.returncode == 2
    assert "waited marker is unsafe" in result.stderr
    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_wait_rejects_a_fifo_waited_marker_without_blocking(tmp_path: Path) -> None:
    assert _job(tmp_path, "run", "waited-fifo", "--", "true").returncode == 0
    job_dir = tmp_path / "jobs" / "waited-fifo"
    (job_dir / "waited").unlink()
    os.mkfifo(job_dir / "waited")

    result = _job(tmp_path, "wait", "waited-fifo", timeout=5)

    assert result.returncode == 2
    assert "waited marker is unsafe" in result.stderr


def test_run_reports_a_durable_canonical_log_path(tmp_path: Path) -> None:
    result = _job(tmp_path, "run", "durable-log", "--", "true")

    assert result.returncode == 0
    assert f"full log: {tmp_path / 'jobs' / 'durable-log' / 'log'}" in result.stdout
    assert "/proc/" not in result.stdout


def test_run_timeout_reports_a_durable_canonical_log_path(tmp_path: Path) -> None:
    try:
        result = _job(
            tmp_path,
            "run",
            "durable-timeout-log",
            "--timeout",
            "0",
            "--",
            "sleep",
            "30",
        )

        assert result.returncode == 124
        assert (
            f"full log: {tmp_path / 'jobs' / 'durable-timeout-log' / 'log'}"
            in result.stderr
        )
        assert "/proc/" not in result.stderr
    finally:
        _job(tmp_path, "clean", "--all")


def test_public_wait_rejects_internal_directory_authority_options(
    tmp_path: Path,
) -> None:
    arbitrary = tmp_path / "arbitrary-authority"
    arbitrary.mkdir()
    (arbitrary / "exit_code").write_text("0\n", encoding="ascii")
    authority_fd = os.open(arbitrary, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = subprocess.run(
            [
                str(SCRIPT),
                "wait",
                "public",
                "--authority-fd",
                str(authority_fd),
                "--expected-pid",
                "999999",
            ],
            cwd=REPO_ROOT,
            env={
                "PATH": "/usr/bin:/bin",
                "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
            },
            pass_fds=(authority_fd,),
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        os.close(authority_fd)

    assert result.returncode == 2
    assert "internal wait authority is unavailable" in result.stderr
    assert not (arbitrary / "waited").exists()


def _wait_until_done(tmp_path: Path, name: str, *, script: Path = SCRIPT) -> None:
    for _ in range(100):
        if "DONE" in _job(tmp_path, "status", name, script=script).stdout:
            return
        time.sleep(0.02)
    pytest.fail(f"job {name!r} did not finish")


def _isolated_job_script(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    script = repo / "scripts" / "util" / "job.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    shutil.copy2(REPO_ROOT / "scripts" / "util" / "job_store.py", script.parent)
    shutil.copy2(
        REPO_ROOT / "scripts" / "util" / "trusted_executable.py", script.parent
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "util" / "repro_connect_guard.py", script.parent
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return script


def _instrumented_job_script(tmp_path: Path, *, marker: str, replacement: str) -> Path:
    """Copy the real runner and add one process-death point for interruption tests."""
    script = _isolated_job_script(tmp_path)
    source = script.read_text(encoding="utf-8")
    assert source.count(marker) == 1
    script.write_text(source.replace(marker, replacement, 1), encoding="utf-8")
    return script


def _write_final_ci_repro_stub(
    script: Path,
    *,
    signed: bool,
    probe_provider_sandbox: bool = False,
    substitute_authority: bool = False,
    spawn_delayed_descendant: bool = False,
    probe_manager_escape: bool = False,
    delay_seconds: int = 0,
) -> Path:
    final_ci_gate = script.with_name("final_ci_gate.py")
    provider_probe = (
        "completed = subprocess.run([\n"
        "    '/usr/bin/bwrap', '--die-with-parent', '--new-session',\n"
        "    '--unshare-user', '--unshare-pid', '--ro-bind', '/usr', '/usr',\n"
        "    '--symlink', 'usr/bin', '/bin', '--proc', '/proc', '--dev', '/dev',\n"
        "    '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',\n"
        "    '--', '/usr/bin/true',\n"
        "], check=False)\n"
        "exit_code = completed.returncode\n"
        if probe_provider_sandbox
        else "exit_code = 0\n"
    )
    delayed_descendant = (
        "subprocess.Popen([\n"
        "    sys.executable, '-c',\n"
        '    "import os,time; from pathlib import Path; time.sleep(0.4); "\n'
        "    \"Path(os.environ['INTELFLO_TEST_DELAYED_MARKER']).write_text('escaped')\",\n"
        "])\n"
        if spawn_delayed_descendant
        else ""
    )
    authority_substitution = (
        "protected_root = Path(os.environ.pop(\n"
        "    'INTELFLO_FINAL_CI_REPRO_PROTECTED_ROOT'\n"
        "))\n"
        "moved_root = protected_root.with_name(protected_root.name + '-moved')\n"
        "protected_root.rename(moved_root)\n"
        "protected_root.mkdir()\n"
        "fake_job = protected_root / os.environ['INTELFLO_JOB_NAME']\n"
        "fake_job.mkdir()\n"
        "(fake_job / 'pid').write_text('999999\\n')\n"
        "(fake_job / 'exit_code').write_text('0\\n')\n"
        if substitute_authority
        else "os.environ.pop('INTELFLO_FINAL_CI_REPRO_PROTECTED_ROOT')\n"
    )
    manager_escape_probe = (
        "manager_environment = dict(os.environ)\n"
        "manager_environment['XDG_RUNTIME_DIR'] = f'/run/user/{os.getuid()}'\n"
        "manager_environment['DBUS_SESSION_BUS_ADDRESS'] = (\n"
        '    f"unix:path=/run/user/{os.getuid()}/bus"\n'
        ")\n"
        "escaped = subprocess.run([\n"
        "    '/usr/bin/systemd-run', '--user', '--wait', '--collect', '--quiet',\n"
        "    '/usr/bin/true',\n"
        "], check=False, env=manager_environment)\n"
        "if escaped.returncode == 0:\n"
        "    raise SystemExit(92)\n"
        "manager_alias = artifact.with_name('manager-bus-alias')\n"
        "manager_alias.symlink_to(f'/run/user/{os.getuid()}/bus')\n"
        "alias_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "try:\n"
        "    alias_client.connect(manager_alias.name)\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(93)\n"
        "finally:\n"
        "    alias_client.close()\n"
        if probe_manager_escape
        else ""
    )
    delay = (
        f"subprocess.run(['/usr/bin/sleep', '{delay_seconds}'], check=False)\n"
        if delay_seconds
        else ""
    )
    signature_line = (
        "terminal['job_authorization_hmac_sha256'] = "
        "hmac.new(key, canonical, hashlib.sha256).hexdigest()"
        if signed
        else "pass"
    )
    final_ci_gate.write_text(
        f"""import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

fd = int(os.environ.pop("INTELFLO_FINAL_CI_REPRO_AUTH_FD"))
try:
    key = os.read(fd, 33)
finally:
    os.close(fd)
assert len(key) == 32
assert "INTELFLO_JOB_EXECUTOR_PID" not in os.environ
{authority_substitution}
assert sys.argv[1:3] == ["--pr", "42"]
signature = sys.argv[sys.argv.index("--execute-repro") + 1]
artifact = Path(sys.argv[sys.argv.index("--terminal-artifact") + 1])
{manager_escape_probe}{delayed_descendant}{delay}{provider_probe}terminal = {{
    "schema_version": "final-ci-repro-terminal-v2",
    "task_id": f"final-ci-repro:{{signature}}",
    "status": "completed" if exit_code == 0 else "failed",
    "pr": 42,
    "failure_signature": signature,
    "head_sha_before": "c" * 40,
    "head_tree_before": "d" * 40,
    "head_sha_after": "c" * 40,
    "head_tree_after": "d" * 40,
    "prescribed_local_command_digest": "e" * 64,
    "exit_code": exit_code,
    "outcome": "not_reproduced" if exit_code == 0 else "reproduced",
}}
canonical = json.dumps(
    terminal, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode()
{signature_line}
artifact.write_text(json.dumps(terminal, sort_keys=True) + "\\n", encoding="utf-8")
raise SystemExit(exit_code)
""",
        encoding="utf-8",
    )
    return final_ci_gate


def _deny_outer_bubblewrap(script: Path, *, allow_systemd: bool = True) -> None:
    fake_systemd = script.with_name("test_systemd_run.py")
    fake_systemd.write_text(
        """#!/usr/bin/python3
import os
import signal
import subprocess
import sys

environment = dict(os.environ)
environment.pop("INTELFLO_JOB_EXECUTOR_PID", None)
working_directory = None
index = 1
while index < len(sys.argv) and sys.argv[index].startswith("--"):
    argument = sys.argv[index]
    if argument.startswith("--setenv="):
        assignment = argument.removeprefix("--setenv=")
        if "=" in assignment:
            name, value = assignment.split("=", 1)
        else:
            name = assignment
            value = os.environ[name]
        environment[name] = value
    elif argument.startswith("--working-directory="):
        working_directory = argument.split("=", 1)[1]
    index += 1

completed = subprocess.Popen(
    sys.argv[index:],
    cwd=working_directory,
    env=environment,
    start_new_session=True,
)
exit_code = completed.wait()
try:
    os.killpg(completed.pid, signal.SIGKILL)
except ProcessLookupError:
    pass
raise SystemExit(exit_code if exit_code >= 0 else 128 + abs(exit_code))
""",
        encoding="utf-8",
    )
    fake_systemd.chmod(0o755)
    systemd_path = (
        fake_systemd
        if os.environ.get("INTELFLO_GUARDIAN_SANDBOX_BOUNDARY") is not None
        else Path("/usr/bin/systemd-run")
    )
    systemd_case = (
        '    if name == "systemd-run":\n'
        f"        return Path({str(systemd_path)!r})\n"
        if allow_systemd
        else ""
    )
    script.with_name("trusted_executable.py").write_text(
        f"""from pathlib import Path

class TrustedExecutableError(RuntimeError):
    pass

def system_executable(name):
{systemd_case}    raise TrustedExecutableError(f"unexpected outer sandbox: {{name}}")
""",
        encoding="utf-8",
    )


def _run_final_ci_repro_job(
    tmp_path: Path,
    *,
    signed: bool,
    probe_provider_sandbox: bool = False,
    substitute_authority: bool = False,
    spawn_delayed_descendant: bool = False,
    probe_manager_escape: bool = False,
    delay_seconds: int = 0,
    allow_systemd: bool = True,
    run_timeout: str = "30",
    extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    final_ci_gate = _write_final_ci_repro_stub(
        script,
        signed=signed,
        probe_provider_sandbox=probe_provider_sandbox,
        substitute_authority=substitute_authority,
        spawn_delayed_descendant=spawn_delayed_descendant,
        probe_manager_escape=probe_manager_escape,
        delay_seconds=delay_seconds,
    )
    _deny_outer_bubblewrap(script, allow_systemd=allow_systemd)
    failure_signature = "a" * 64
    artifact = repo / "repro-result.json"
    name = "final-ci-repro-test"
    result = _job(
        tmp_path,
        "run",
        name,
        "--timeout",
        run_timeout,
        "--run-id",
        "sr_" + "b" * 32,
        "--task-id",
        f"final-ci-repro:{failure_signature}",
        "--terminal-artifact",
        str(artifact),
        "--",
        "/usr/bin/python3",
        str(final_ci_gate),
        "--pr",
        "42",
        "--repo",
        str(repo),
        "--execute-repro",
        failure_signature,
        "--terminal-artifact",
        str(artifact),
        *extra_args,
        script=script,
        env_overrides={
            **{
                name: os.environ[name]
                for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
                if name in os.environ
            },
            **(
                {"INTELFLO_TEST_DELAYED_MARKER": str(repo / "delayed-descendant")}
                if spawn_delayed_descendant
                else {}
            ),
        },
    )
    job_dir = tmp_path / "jobs" / name
    log_path = job_dir / "log"
    if log_path.exists():
        log = log_path.read_text(encoding="utf-8")
        if any(signature in log for signature in REPRO_GUARD_UNAVAILABLE_ERRORS):
            pytest.skip("reproduction guard prerequisites are unavailable on this host")

    return result, artifact, job_dir


@requires_nested_user_namespace
def test_signed_final_ci_repro_runs_provider_sandbox_without_outer_user_namespace(
    tmp_path: Path,
) -> None:
    result, _, job_dir = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        probe_provider_sandbox=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    envelope = json.loads(
        (job_dir / "terminal-envelope.json").read_text(encoding="utf-8")
    )
    terminal = envelope["terminal"]
    assert len(terminal["job_authorization_hmac_sha256"]) == 64
    assert envelope["command_exit_code"] == 0
    assert terminal["outcome"] == "not_reproduced"


@requires_nested_user_namespace
def test_signed_final_ci_repro_reaps_delayed_descendants(tmp_path: Path) -> None:
    result, artifact, _ = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        spawn_delayed_descendant=True,
        extra_args=(),
    )
    marker = artifact.parent / "delayed-descendant"

    assert result.returncode == 0, result.stdout + result.stderr
    time.sleep(0.6)
    assert not marker.exists()


@requires_nested_user_namespace
def test_signed_final_ci_repro_timeout_reaps_its_secure_executor(
    tmp_path: Path,
) -> None:
    result, artifact, job_dir = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        delay_seconds=10,
        run_timeout="1",
    )
    executor_pid = int((job_dir / "pid").read_text(encoding="ascii"))

    assert result.returncode == 124, result.stdout + result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(executor_pid, 0)
    assert not artifact.exists()
    assert not (job_dir / "terminal-envelope.json").exists()


@requires_nested_user_namespace
def test_signed_final_ci_repro_rejects_authority_path_substitution(
    tmp_path: Path,
) -> None:
    result, _, job_dir = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        substitute_authority=True,
    )

    assert result.returncode == 125, result.stdout + result.stderr
    assert not (job_dir / "terminal-envelope.json").exists()
    moved_log = (
        job_dir.parent.with_name(job_dir.parent.name + "-moved") / job_dir.name / "log"
    )
    assert "authority path changed" in moved_log.read_text(encoding="utf-8")


@requires_nested_user_namespace
def test_signed_final_ci_repro_blocks_user_manager_escape(tmp_path: Path) -> None:
    result, _, job_dir = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        probe_manager_escape=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (job_dir / "terminal-envelope.json").exists()


@requires_nested_user_namespace
def test_final_ci_repro_refuses_an_unsigned_terminal_artifact(tmp_path: Path) -> None:
    result, _, job_dir = _run_final_ci_repro_job(tmp_path, signed=False)

    assert result.returncode == 125, result.stdout + result.stderr
    assert not (job_dir / "terminal-envelope.json").exists()
    assert "authorization" in (job_dir / "log").read_text(encoding="utf-8")


def test_final_ci_repro_fails_closed_without_systemd_boundary(tmp_path: Path) -> None:
    result, artifact, job_dir = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        allow_systemd=False,
    )

    assert result.returncode == 125, result.stdout + result.stderr
    assert not artifact.exists()
    assert not (job_dir / "terminal-envelope.json").exists()
    assert "requires protected systemd-run" in (job_dir / "log").read_text(
        encoding="utf-8"
    )


def test_final_ci_repro_task_refuses_noncanonical_command_authority(
    tmp_path: Path,
) -> None:
    result, artifact, job_dir = _run_final_ci_repro_job(
        tmp_path,
        signed=True,
        extra_args=("--unexpected-command-authority",),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert not artifact.exists()
    log = (job_dir / "log").read_text(encoding="utf-8")
    assert "canonical executor command" in log


def _blocked_dispatch_evidence(
    repo: Path,
    *,
    run_id: str,
    task_id: str,
    terminal_overrides: dict[str, object] | None = None,
) -> tuple[Path, dict[str, object]]:
    artifact = repo / ".audit" / "dispatch" / "results" / f"{task_id}.json"
    artifact.parent.mkdir(parents=True)
    artifact_bytes = json.dumps(
        {"task_id": task_id, "status": "blocked"}, sort_keys=True
    ).encode()
    artifact.write_bytes(artifact_bytes)
    terminal_record: dict[str, object] = {
        "type": "attempt-terminal",
        "schema_version": "dispatch-telemetry-v9",
        "policy_version": "2026-08-06-v10",
        "runtime_contract_version": 4,
        "task_id": task_id,
        "run_id": run_id,
        "status": "blocked",
        "exit_code": 0,
        "result_artifact": f".audit/dispatch/results/{task_id}.json",
        "result_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        **(terminal_overrides or {}),
    }
    telemetry = artifact.parent.parent / "2026-08-13.jsonl"
    telemetry.write_text(json.dumps(terminal_record) + "\n", encoding="utf-8")
    return artifact, terminal_record


@requires_nested_user_namespace
def test_bound_job_reconciles_only_from_its_terminal_dispatch_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    run_id = "sr_" + "a" * 32
    started = _job(
        tmp_path,
        "start",
        "bound",
        "--run-id",
        run_id,
        "--task-id",
        "dispatch-closeout",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
    )
    assert started.returncode == 0, started.stderr
    _wait_until_done(tmp_path, "bound")
    artifact.unlink()

    reconciled = _job(tmp_path, "reconcile", "bound")

    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(
        (tmp_path / "jobs" / "bound" / "reconciliation.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["schema_version"] == "job-reconciliation-v2"
    assert receipt["terminal_envelope_sha256"]
    assert receipt["terminal_artifact_sha256"]
    assert receipt["terminal_status"] == "completed"
    assert receipt["source_terminal_artifact"] == str(artifact.resolve())
    assert "terminal_authority" not in receipt
    assert _job(tmp_path, "check").returncode == 0
    bindings = _job(tmp_path, "binding-files", "--run-id", run_id)
    assert bindings.stdout.strip() == str(tmp_path / "jobs" / "bound" / "binding.json")
    binding = json.loads(
        (tmp_path / "jobs" / "bound" / "binding.json").read_text(encoding="utf-8")
    )
    assert binding == {
        "schema_version": "job-binding-v2",
        "name": "bound",
        "run_id": run_id,
        "task_id": "dispatch-closeout",
        "terminal_envelope": str(
            (tmp_path / "jobs" / "bound" / "terminal-envelope.json").resolve()
        ),
    }


def test_v2_reconciliation_rejects_external_artifact_override(tmp_path: Path) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    assert (
        _job(
            tmp_path,
            "start",
            "bound",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "dispatch-closeout",
            "--terminal-artifact",
            str(artifact),
            "--",
            "true",
        ).returncode
        == 0
    )
    _wait_until_done(tmp_path, "bound")

    result = _job(
        tmp_path,
        "reconcile",
        "bound",
        "--terminal-artifact",
        str(artifact),
    )

    assert result.returncode == 2
    assert "v2" in result.stderr


@requires_nested_user_namespace
@pytest.mark.parametrize("runtime_contract_version", [4, None])
def test_bound_job_reconciles_blocked_dispatch_from_terminal_telemetry(
    tmp_path: Path,
    runtime_contract_version: int | None,
) -> None:
    run_id = "sr_" + "a" * 32
    task_id = "dispatch-blocked"
    script = _isolated_job_script(tmp_path)
    artifact, terminal_record = _blocked_dispatch_evidence(
        script.parents[2],
        run_id=run_id,
        task_id=task_id,
        terminal_overrides={"runtime_contract_version": runtime_contract_version},
    )
    telemetry = artifact.parent.parent / "2026-08-13.jsonl"
    telemetry.write_text(
        json.dumps(terminal_record) + "\n" + json.dumps(terminal_record) + "\n",
        encoding="utf-8",
    )
    started = _job(
        tmp_path,
        "start",
        "bound",
        "--run-id",
        run_id,
        "--task-id",
        task_id,
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
        script=script,
    )
    assert started.returncode == 0, started.stderr
    _wait_until_done(tmp_path, "bound", script=script)

    reconciled = _job(tmp_path, "reconcile", "bound", script=script)

    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(reconciled.stdout)
    assert receipt["exit_code"] == 0
    assert receipt["terminal_status"] == "blocked"
    assert receipt["terminal_authority"] == "dispatcher-attempt-terminal"
    assert (
        receipt["terminal_record_sha256"]
        == hashlib.sha256(
            json.dumps(terminal_record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


@requires_nested_user_namespace
def test_blocked_reconciliation_anchors_telemetry_to_the_bound_artifact(
    tmp_path: Path,
) -> None:
    run_id = "sr_" + "a" * 32
    task_id = "dispatch-blocked-authority"
    script = _isolated_job_script(tmp_path)
    repo = script.parents[2]
    artifact, terminal_record = _blocked_dispatch_evidence(
        repo, run_id=run_id, task_id=task_id
    )
    telemetry = artifact.parent.parent / "2026-08-14.jsonl"
    telemetry.write_text(json.dumps(terminal_record) + "\n", encoding="utf-8")
    started = _job(
        tmp_path,
        "start",
        "blocked-authority",
        "--run-id",
        run_id,
        "--task-id",
        task_id,
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
        script=script,
    )
    assert started.returncode == 0, started.stderr
    _wait_until_done(tmp_path, "blocked-authority", script=script)

    attacker = tmp_path / "attacker"
    attacker.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=attacker, check=True)
    (repo / ".git").rename(repo / ".git-trusted")
    (repo / ".git").write_text(f"gitdir: {attacker / '.git'}\n", encoding="utf-8")

    reconciled = _job(tmp_path, "reconcile", "blocked-authority", script=script)

    assert reconciled.returncode == 0, reconciled.stderr
    assert json.loads(reconciled.stdout)["terminal_status"] == "blocked"


@pytest.mark.parametrize(
    "compatible_policy",
    ["2026-07-24-v9", "2026-08-06-v10", "2026-08-17-v11"],
)
def test_legacy_v1_blocked_recovery_keeps_terminal_telemetry_authority(
    tmp_path: Path,
    compatible_policy: str,
) -> None:
    run_id = "sr_" + "a" * 32
    task_id = "dispatch-legacy-blocked"
    script = _isolated_job_script(tmp_path)
    artifact, terminal_record = _blocked_dispatch_evidence(
        script.parents[2],
        run_id=run_id,
        task_id=task_id,
        terminal_overrides={"policy_version": compatible_policy},
    )
    job_dir = tmp_path / "jobs" / "legacy-blocked"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-blocked",
                "run_id": run_id,
                "task_id": task_id,
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="utf-8")

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-blocked",
        "--terminal-artifact",
        str(artifact),
        script=script,
    )

    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(reconciled.stdout)
    assert receipt["schema_version"] == "job-reconciliation-v1"
    assert receipt["terminal_authority"] == "dispatcher-attempt-terminal"
    assert (
        receipt["terminal_record_sha256"]
        == hashlib.sha256(
            json.dumps(terminal_record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


@requires_nested_user_namespace
def test_bound_job_rejects_blocked_result_without_terminal_telemetry(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-blocked", "status": "blocked"}),
        encoding="utf-8",
    )
    started = _job(
        tmp_path,
        "start",
        "bound",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "dispatch-blocked",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
    )
    assert started.returncode == 0
    _wait_until_done(tmp_path, "bound")

    reconciled = _job(tmp_path, "reconcile", "bound")

    assert reconciled.returncode == 1
    assert "dispatcher terminal telemetry" in reconciled.stderr


@requires_nested_user_namespace
@pytest.mark.parametrize(
    "terminal_overrides",
    [
        {"schema_version": "dispatch-telemetry-v99"},
        {"policy_version": "2099-01-01-v99"},
        {"runtime_contract_version": 99},
        {"exit_code": False},
    ],
)
def test_bound_job_rejects_noncurrent_blocked_terminal_telemetry(
    tmp_path: Path,
    terminal_overrides: dict[str, object],
) -> None:
    run_id = "sr_" + "a" * 32
    task_id = "dispatch-blocked"
    script = _isolated_job_script(tmp_path)
    artifact, _ = _blocked_dispatch_evidence(
        script.parents[2],
        run_id=run_id,
        task_id=task_id,
        terminal_overrides=terminal_overrides,
    )
    started = _job(
        tmp_path,
        "start",
        "bound",
        "--run-id",
        run_id,
        "--task-id",
        task_id,
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
        script=script,
    )
    assert started.returncode == 0
    _wait_until_done(tmp_path, "bound", script=script)

    reconciled = _job(
        tmp_path,
        "reconcile",
        "bound",
        script=script,
    )

    assert reconciled.returncode == 1
    assert "dispatcher terminal telemetry" in reconciled.stderr


def test_legacy_v1_binding_keeps_explicit_artifact_recovery(tmp_path: Path) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "legacy-task", "status": "completed"}),
        encoding="utf-8",
    )
    job_dir = tmp_path / "jobs" / "legacy"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="utf-8")

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 0, reconciled.stderr
    assert json.loads(reconciled.stdout)["schema_version"] == "job-reconciliation-v1"


def test_legacy_v1_reconciliation_rejects_a_symlinked_exit_code(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "legacy-task", "status": "completed"}),
        encoding="utf-8",
    )
    attacker_exit = tmp_path / "attacker-exit-code"
    attacker_exit.write_text("0\n", encoding="ascii")
    job_dir = tmp_path / "jobs" / "legacy-symlink"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-symlink",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").symlink_to(attacker_exit)

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-symlink",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 1
    assert "exit code cannot be a symlink" in reconciled.stderr
    assert not (job_dir / "reconciliation.json").exists()


@pytest.mark.parametrize("unsafe_name", ["exit_code", "reconciliation.json"])
def test_legacy_v1_reconciliation_rejects_fifo_inputs_without_blocking(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "legacy-task", "status": "completed"}),
        encoding="utf-8",
    )
    job_dir = tmp_path / "jobs" / "legacy-fifo"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-fifo",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    if unsafe_name != "exit_code":
        (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    os.mkfifo(job_dir / unsafe_name)

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-fifo",
        "--terminal-artifact",
        str(artifact),
        timeout=5,
    )

    assert reconciled.returncode == 1
    assert "safe file" in reconciled.stderr


def test_legacy_v1_reconciliation_rejects_a_fifo_artifact_without_blocking(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    os.mkfifo(artifact)
    job_dir = tmp_path / "jobs" / "legacy-artifact-fifo"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-artifact-fifo",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-artifact-fifo",
        "--terminal-artifact",
        str(artifact),
        timeout=5,
    )

    assert reconciled.returncode == 1
    assert "safe file" in reconciled.stderr


def test_reconciliation_rejects_a_fifo_binding_without_blocking(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "binding-fifo"
    job_dir.mkdir(parents=True)
    os.mkfifo(job_dir / "binding.json")

    reconciled = _job(tmp_path, "reconcile", "binding-fifo", timeout=5)

    assert reconciled.returncode == 2
    assert "no delivery-run binding" in reconciled.stderr


@pytest.mark.parametrize(
    "unsafe_name", ["terminal-envelope.json", "terminal-envelope.sha256"]
)
def test_v2_reconciliation_rejects_fifo_envelope_inputs_without_blocking(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    job_dir = tmp_path / "jobs" / "v2-fifo"
    job_dir.mkdir(parents=True)
    envelope = job_dir / "terminal-envelope.json"
    digest = job_dir / "terminal-envelope.sha256"
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v2",
                "name": "v2-fifo",
                "run_id": "sr_" + "a" * 32,
                "task_id": "v2-task",
                "terminal_envelope": str(envelope.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    if unsafe_name != envelope.name:
        envelope.write_text("{}\n", encoding="utf-8")
    if unsafe_name != digest.name:
        digest.write_text("0" * 64 + "\n", encoding="ascii")
    os.mkfifo(job_dir / unsafe_name)

    reconciled = _job(tmp_path, "reconcile", "v2-fifo", timeout=5)

    assert reconciled.returncode == 1
    assert "safe file" in reconciled.stderr


@pytest.mark.parametrize(
    "receipt_name",
    ["reconciliation.json", "reconciliation.json.tmp"],
)
def test_legacy_v1_reconciliation_rejects_symlinked_receipt_paths(
    tmp_path: Path,
    receipt_name: str,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "legacy-task", "status": "completed"}),
        encoding="utf-8",
    )
    target = tmp_path / "receipt-target"
    target.write_text("preserve\n", encoding="utf-8")
    job_dir = tmp_path / "jobs" / "legacy-receipt-symlink"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-receipt-symlink",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    (job_dir / receipt_name).symlink_to(target)

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-receipt-symlink",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 1
    assert "reconciliation" in reconciled.stderr
    assert "symlink" in reconciled.stderr
    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_legacy_v1_reconciliation_preserves_an_existing_receipt_temporary(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "legacy-task", "status": "completed"}),
        encoding="utf-8",
    )
    job_dir = tmp_path / "jobs" / "legacy-receipt-temporary"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-receipt-temporary",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    temporary = job_dir / "reconciliation.json.tmp"
    temporary.write_text("preserve\n", encoding="utf-8")

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-receipt-temporary",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 1
    assert "temporary" in reconciled.stderr
    assert temporary.read_text(encoding="utf-8") == "preserve\n"
    assert not (job_dir / "reconciliation.json").exists()


def test_legacy_v1_reconciliation_rejects_a_symlinked_waited_marker(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "legacy-task", "status": "completed"}),
        encoding="utf-8",
    )
    target = tmp_path / "waited-target"
    target.write_text("preserve\n", encoding="utf-8")
    job_dir = tmp_path / "jobs" / "legacy-waited-symlink"
    job_dir.mkdir(parents=True)
    (job_dir / "binding.json").write_text(
        json.dumps(
            {
                "schema_version": "job-binding-v1",
                "name": "legacy-waited-symlink",
                "run_id": "sr_" + "a" * 32,
                "task_id": "legacy-task",
                "terminal_artifact": str(artifact.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    (job_dir / "waited").symlink_to(target)

    reconciled = _job(
        tmp_path,
        "reconcile",
        "legacy-waited-symlink",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 2
    assert "waited marker is unsafe" in reconciled.stderr
    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_bound_job_rejects_missing_terminal_before_exposing_success(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    assert (
        _job(
            tmp_path,
            "start",
            "bound",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "missing-task",
            "--terminal-artifact",
            str(missing),
            "--",
            "true",
        ).returncode
        == 0
    )
    _wait_until_done(tmp_path, "bound")
    job_dir = tmp_path / "jobs" / "bound"

    assert (job_dir / "exit_code").read_text(encoding="utf-8").strip() == "125"
    assert not (job_dir / "terminal-envelope.json").exists()
    assert "unreadable" in (job_dir / "log").read_text(encoding="utf-8")


def test_bound_job_replaces_command_failure_when_terminal_sealing_fails(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    assert (
        _job(
            tmp_path,
            "start",
            "bound",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "missing-task",
            "--terminal-artifact",
            str(missing),
            "--",
            "sh",
            "-c",
            "exit 7",
        ).returncode
        == 0
    )
    _wait_until_done(tmp_path, "bound")
    job_dir = tmp_path / "jobs" / "bound"

    assert (job_dir / "exit_code").read_text(encoding="utf-8").strip() == "125"
    assert not (job_dir / "terminal-envelope.json").exists()


@requires_nested_user_namespace
def test_bound_job_rejects_tampered_terminal_envelope(tmp_path: Path) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )
    assert (
        _job(
            tmp_path,
            "start",
            "bound",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "dispatch-closeout",
            "--terminal-artifact",
            str(artifact),
            "--",
            "true",
        ).returncode
        == 0
    )
    _wait_until_done(tmp_path, "bound")
    envelope = tmp_path / "jobs" / "bound" / "terminal-envelope.json"
    envelope.chmod(0o600)
    envelope.write_text("{}\n", encoding="utf-8")

    reconciled = _job(tmp_path, "reconcile", "bound")

    assert reconciled.returncode == 1
    assert "digest" in reconciled.stderr


def test_bound_job_rejects_a_different_terminal_task(tmp_path: Path) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "foreign", "status": "completed"}),
        encoding="utf-8",
    )
    started = _job(
        tmp_path,
        "start",
        "bound",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "expected",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
    )
    assert started.returncode == 0
    _wait_until_done(tmp_path, "bound")

    job_dir = tmp_path / "jobs" / "bound"
    assert (job_dir / "exit_code").read_text(encoding="utf-8").strip() == "125"
    assert "task_id" in (job_dir / "log").read_text(encoding="utf-8")
    assert _job(tmp_path, "check").returncode == 1


def test_bound_job_rejects_a_binding_name_that_differs_from_its_job(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "expected", "status": "completed"}),
        encoding="utf-8",
    )
    started = _job(
        tmp_path,
        "start",
        "bound",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "expected",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
    )
    assert started.returncode == 0
    _wait_until_done(tmp_path, "bound")
    binding_path = tmp_path / "jobs" / "bound" / "binding.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["name"] = "foreign"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")

    reconciled = _job(tmp_path, "reconcile", "bound")

    assert reconciled.returncode == 1
    assert "binding name" in reconciled.stderr


def test_binding_enumeration_fails_closed_on_malformed_job_authority(
    tmp_path: Path,
) -> None:
    binding = tmp_path / "jobs" / "broken" / "binding.json"
    binding.parent.mkdir(parents=True)
    binding.write_text("not json\n", encoding="utf-8")

    result = _job(tmp_path, "binding-files", "--run-id", "sr_" + "a" * 32)

    assert result.returncode == 1
    assert "invalid job binding" in result.stderr


@pytest.mark.parametrize("option", ["--run-id", "--task-id", "--terminal-artifact"])
def test_bound_job_missing_option_value_is_a_usage_error(
    tmp_path: Path, option: str
) -> None:
    result = _job(tmp_path, "start", "bound", option)

    assert result.returncode == 2
    assert "requires a value" in result.stderr


def _write_orphaned_v2_binding(
    tmp_path: Path,
    *,
    name: str = "orphaned-bound",
    pid: str | None = None,
) -> tuple[Path, dict[str, str]]:
    job_dir = tmp_path / "jobs" / name
    job_dir.mkdir(parents=True, mode=0o700)
    binding = {
        "schema_version": "job-binding-v2",
        "name": name,
        "run_id": "sr_" + "a" * 32,
        "task_id": "interrupted-review",
        "terminal_envelope": str((job_dir / "terminal-envelope.json").resolve()),
    }
    (job_dir / "binding.json").write_text(
        json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (job_dir / "log").write_text("", encoding="utf-8")
    if pid is not None:
        (job_dir / "pid").write_text(pid + "\n", encoding="ascii")
    return job_dir, binding


def _write_sealed_v2_envelope(
    tmp_path: Path,
    job_dir: Path,
    binding: dict[str, str],
    *,
    exit_code: int = 0,
) -> None:
    artifact = tmp_path / f"{binding['name']}-terminal.json"
    terminal = {
        "task_id": binding["task_id"],
        "status": "completed" if exit_code == 0 else "failed",
    }
    artifact_bytes = json.dumps(terminal, sort_keys=True).encode()
    artifact.write_bytes(artifact_bytes)
    envelope = {
        "schema_version": "job-terminal-envelope-v2",
        "name": binding["name"],
        "run_id": binding["run_id"],
        "task_id": binding["task_id"],
        "source_terminal_artifact": str(artifact.resolve()),
        "terminal_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "command_exit_code": exit_code,
        "terminal": terminal,
    }
    envelope_bytes = (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode()
    (job_dir / "terminal-envelope.json").write_bytes(envelope_bytes)
    (job_dir / "terminal-envelope.sha256").write_text(
        hashlib.sha256(envelope_bytes).hexdigest() + "\n",
        encoding="ascii",
    )


def _write_dispatcher_stub(script: Path, artifact: Path, *, task_id: str) -> Path:
    dispatcher = script.with_name("agent_dispatch.py")
    dispatcher.write_text(
        "import json\n"
        "from pathlib import Path\n"
        f"Path({str(artifact)!r}).write_text("
        f"json.dumps({{'task_id': {task_id!r}, 'status': 'completed'}}), "
        "encoding='utf-8')\n",
        encoding="utf-8",
    )
    return dispatcher


def test_live_death_before_binding_leaves_only_replaceable_prebinding_state(
    tmp_path: Path,
) -> None:
    marker = '\tif [ -n "$run_id" ]; then'
    script = _instrumented_job_script(
        tmp_path,
        marker=marker,
        replacement='\tkill -KILL "$BASHPID"\n' + marker,
    )
    artifact = tmp_path / "terminal.json"

    interrupted = _job(
        tmp_path,
        "start",
        "before-binding",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "pilot-before-binding",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
        script=script,
    )

    assert interrupted.returncode < 0
    job_dir = tmp_path / "jobs" / "before-binding"
    assert job_dir.is_dir()
    assert not (job_dir / "binding.json").exists()
    replacement = _job(
        tmp_path, "run", "before-binding", "--timeout", "30", "--", "true"
    )
    assert replacement.returncode == 0, replacement.stderr


def test_live_death_after_binding_is_automatically_reconcilable(
    tmp_path: Path,
) -> None:
    marker = '\tif [ "${HOLD_AUTHORITY_FOR_RUN:-0}" = "1" ]; then'
    script = _instrumented_job_script(
        tmp_path,
        marker=marker,
        replacement='\tkill -KILL "$BASHPID"\n' + marker,
    )
    artifact = tmp_path / "terminal.json"

    interrupted = _job(
        tmp_path,
        "start",
        "after-binding",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "pilot-after-binding",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
        script=script,
    )

    assert interrupted.returncode < 0
    reconciled = _job(tmp_path, "reconcile", "after-binding", script=script)
    assert reconciled.returncode == 0, reconciled.stderr
    assert json.loads(reconciled.stdout)["reason"] == "startup-failure"


@pytest.mark.parametrize(
    ("marker", "replacement", "expected_temporaries"),
    [
        (
            '    exit_fd = reserve("exit_code.tmp")',
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
            '    exit_fd = reserve("exit_code.tmp")',
            (),
        ),
        (
            "    owns_launch = True",
            "    os.kill(os.getpid(), signal.SIGKILL)\n    owns_launch = True",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
            ),
        ),
        (
            "    final_code = completed.returncode",
            "    final_code = completed.returncode\n"
            "    os.kill(os.getpid(), signal.SIGKILL)",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
            ),
        ),
    ],
    ids=("after-pid", "after-reservations", "after-child-exit"),
)
def test_live_supervisor_death_yields_typed_loss_without_promoting_temps(
    tmp_path: Path,
    marker: str,
    replacement: str,
    expected_temporaries: tuple[str, ...],
) -> None:
    script = _instrumented_job_script(tmp_path, marker=marker, replacement=replacement)
    artifact = tmp_path / "terminal.json"
    dispatcher = _write_dispatcher_stub(
        script, artifact, task_id="pilot-supervisor-loss"
    )

    started = _job(
        tmp_path,
        "start",
        "live-loss",
        "--run-id",
        "sr_" + "a" * 32,
        "--task-id",
        "pilot-supervisor-loss",
        "--terminal-artifact",
        str(artifact),
        "--",
        "/usr/bin/python3",
        str(dispatcher),
        "run",
        script=script,
    )
    assert started.returncode == 0, started.stderr
    _wait_until_done(tmp_path, "live-loss", script=script)
    job_dir = tmp_path / "jobs" / "live-loss"
    assert all((job_dir / name).exists() for name in expected_temporaries)

    waited = _job(tmp_path, "wait", "live-loss", script=script)
    assert waited.returncode == 1
    assert "run 'reconcile live-loss'" in waited.stderr
    assert not (job_dir / "waited").exists()
    assert (
        _job(tmp_path, "start", "live-loss", "--", "true", script=script).returncode
        == 2
    )

    reconciled = _job(tmp_path, "reconcile", "live-loss", script=script)
    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(reconciled.stdout)
    assert receipt["schema_version"] == "job-supervisor-loss-v1"
    assert receipt["reason"] == "supervisor-loss"
    assert "terminal_status" not in receipt
    assert not (job_dir / "exit_code").exists()


def test_wait_does_not_recommend_reconcile_for_an_unbound_job(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "unbound-loss"
    job_dir.mkdir(parents=True)
    (job_dir / "cmd").write_text("true\n", encoding="utf-8")
    (job_dir / "log").write_text("lost\n", encoding="utf-8")

    waited = _job(tmp_path, "wait", "unbound-loss")

    assert waited.returncode == 1
    assert "reconcile" not in waited.stderr
    assert "inspect its log, then clean --all" in waited.stderr


def test_reconcile_recovers_a_sealed_envelope_when_exit_publication_is_lost(
    tmp_path: Path,
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999997")
    _write_sealed_v2_envelope(tmp_path, job_dir, binding)

    reconciled = _job(tmp_path, "reconcile", binding["name"])

    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(reconciled.stdout)
    assert receipt["schema_version"] == "job-reconciliation-v2"
    assert receipt["exit_code"] == 0
    assert receipt["terminal_status"] == "completed"
    assert not (job_dir / "exit_code").exists()


def test_waited_bound_authority_survives_clean_and_name_reuse(
    tmp_path: Path,
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999997")
    _write_sealed_v2_envelope(tmp_path, job_dir, binding)
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    (job_dir / "waited").touch()

    cleaned = _job(tmp_path, "clean")
    restarted = _job(tmp_path, "start", binding["name"], "--", "true")

    assert cleaned.returncode == 0, cleaned.stderr
    assert restarted.returncode == 2
    assert "unreaped result" in restarted.stderr
    assert (job_dir / "binding.json").exists()
    assert (job_dir / "terminal-envelope.json").exists()


def test_status_probe_does_not_create_a_lifecycle_lease(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "probe-only"
    job_dir.mkdir(parents=True)
    (job_dir / "cmd").write_text("true\n", encoding="utf-8")

    status = _job(tmp_path, "status", "probe-only")

    assert status.returncode == 1
    assert "DONE" in status.stdout
    assert not (tmp_path / "jobs" / ".leases" / "probe-only.lock").exists()


def test_status_fails_closed_when_live_job_evidence_has_no_lease(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "missing-lease"
    job_dir.mkdir(parents=True)
    (job_dir / "cmd").write_text("sleep 30\n", encoding="utf-8")
    (job_dir / "pid").write_text("999997\n", encoding="ascii")

    status = _job(tmp_path, "status", "missing-lease")

    assert status.returncode == 0
    assert "RUNNING" in status.stdout
    assert not (tmp_path / "jobs" / ".leases" / "missing-lease.lock").exists()


def test_status_does_not_execute_a_path_shadowed_flock(tmp_path: Path) -> None:
    attacker = tmp_path / "attacker-bin"
    attacker.mkdir()
    marker = tmp_path / "fake-flock-ran"
    fake_flock = attacker / "flock"
    fake_flock.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    started = _job(tmp_path, "start", "live-probe", "--", "sleep", "30")
    assert started.returncode == 0, started.stderr

    try:
        status = _job(
            tmp_path,
            "status",
            "live-probe",
            env_overrides={"PATH": f"{attacker}:/usr/bin:/bin"},
        )

        assert status.returncode == 0
        assert "RUNNING" in status.stdout
        assert not marker.exists()
    finally:
        _job(tmp_path, "clean", "--all", timeout=20)


def test_status_does_not_execute_a_path_shadowed_python(tmp_path: Path) -> None:
    attacker = tmp_path / "attacker-bin"
    attacker.mkdir()
    marker = tmp_path / "fake-python-ran"
    fake_python = attacker / "python3"
    fake_python.write_text(
        f'#!/bin/sh\ntouch {marker}\nexec /usr/bin/python3 "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    started = _job(tmp_path, "start", "python-probe", "--", "sleep", "30")
    assert started.returncode == 0, started.stderr

    try:
        status = _job(
            tmp_path,
            "status",
            "python-probe",
            env_overrides={"PATH": f"{attacker}:/usr/bin:/bin"},
        )

        assert status.returncode == 0
        assert "RUNNING" in status.stdout
        assert not marker.exists()
    finally:
        _job(tmp_path, "clean", "--all", timeout=20)


def test_status_explains_terminal_evidence_when_the_lease_is_missing(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "terminal-missing-lease"
    job_dir.mkdir(parents=True)
    (job_dir / "cmd").write_text("true\n", encoding="utf-8")
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")
    (job_dir / "binding.json").write_text("{}\n", encoding="utf-8")

    status = _job(tmp_path, "status", "terminal-missing-lease")
    checked = _job(tmp_path, "check")

    assert status.returncode == 0
    assert "lifecycle lease is missing despite terminal evidence" in status.stdout
    assert "run 'reconcile terminal-missing-lease'" in status.stdout
    assert checked.returncode == 1
    assert "lifecycle lease is missing despite terminal evidence" in checked.stderr
    assert "run 'reconcile terminal-missing-lease'" in checked.stderr
    assert not (tmp_path / "jobs" / ".leases" / "terminal-missing-lease.lock").exists()


def test_status_does_not_recommend_reconcile_without_a_delivery_binding(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "unbound-terminal-missing-lease"
    job_dir.mkdir(parents=True)
    (job_dir / "cmd").write_text("true\n", encoding="utf-8")
    (job_dir / "pid").write_text("999997\n", encoding="ascii")
    (job_dir / "exit_code").write_text("0\n", encoding="ascii")

    status = _job(tmp_path, "status", "unbound-terminal-missing-lease")

    assert status.returncode == 0
    assert "lifecycle lease is missing despite terminal evidence" in status.stdout
    assert "reconcile" not in status.stdout
    assert "inspect the job, then clean --all" in status.stdout
    assert not (
        tmp_path / "jobs" / ".leases" / "unbound-terminal-missing-lease.lock"
    ).exists()


def test_lifecycle_writer_tolerates_a_momentary_shared_status_probe(
    tmp_path: Path,
) -> None:
    assert _job(tmp_path, "run", "shared-probe", "--", "true").returncode == 0
    lease = tmp_path / "jobs" / ".leases" / "shared-probe.lock"
    ready = tmp_path / "shared-probe-ready"
    probe = subprocess.Popen(
        [
            "flock",
            "-s",
            str(lease),
            "sh",
            "-c",
            f"touch {ready!s}; sleep 0.05",
        ]
    )
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.005)
    else:
        probe.kill()
        pytest.fail("shared lifecycle probe did not acquire its lease")

    replacement = _job(tmp_path, "run", "shared-probe", "--timeout", "30", "--", "true")
    probe.wait(timeout=5)

    assert replacement.returncode == 0, replacement.stderr


@pytest.mark.parametrize(
    "case",
    [
        "live-before-binding",
        "live-after-binding",
        "live-after-pid",
        "live-after-reservations",
        "live-after-child-exit",
        "orphan-after-binding-before-pid",
        "orphan-after-pid",
        "orphan-dead-pid",
        "orphan-reused-pid",
        "orphan-exit-reserved",
        "orphan-envelope-reserved",
        "orphan-digest-reserved",
        "orphan-child-result",
        "partial-envelope",
        "partial-digest",
        "repeated-reconcile",
        "live-lease-held",
        "prebinding-replacement",
        "nonforced-cleanup",
        "forced-cleanup",
    ],
)
def test_interruption_pilot_matrix_is_20_of_20(tmp_path: Path, case: str) -> None:
    """Keep the accepted 20-case pilot as one executable, countable matrix."""
    if case == "live-before-binding":
        test_live_death_before_binding_leaves_only_replaceable_prebinding_state(
            tmp_path
        )
        return
    if case == "live-after-binding":
        test_live_death_after_binding_is_automatically_reconcilable(tmp_path)
        return
    live_points = {
        "live-after-pid": (
            '    exit_fd = reserve("exit_code.tmp")',
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
            '    exit_fd = reserve("exit_code.tmp")',
            (),
        ),
        "live-after-reservations": (
            "    owns_launch = True",
            "    os.kill(os.getpid(), signal.SIGKILL)\n    owns_launch = True",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
            ),
        ),
        "live-after-child-exit": (
            "    final_code = completed.returncode",
            "    final_code = completed.returncode\n"
            "    os.kill(os.getpid(), signal.SIGKILL)",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
            ),
        ),
    }
    if case in live_points:
        marker, replacement, temporary_names = live_points[case]
        test_live_supervisor_death_yields_typed_loss_without_promoting_temps(
            tmp_path, marker, replacement, temporary_names
        )
        return
    orphan_points = {
        "orphan-after-binding-before-pid": (
            "after-binding-before-pid",
            None,
            (),
            "startup-failure",
        ),
        "orphan-after-pid": ("after-pid-publication", "999991", (), "supervisor-loss"),
        "orphan-dead-pid": ("dead-pid", "999992", (), "supervisor-loss"),
        "orphan-reused-pid": ("reused-live-pid", "current", (), "supervisor-loss"),
        "orphan-exit-reserved": (
            "after-exit-reservation",
            "999993",
            ("exit_code.tmp",),
            "supervisor-loss",
        ),
        "orphan-envelope-reserved": (
            "after-envelope-reservation",
            "999994",
            ("exit_code.tmp", "terminal-envelope.json.tmp"),
            "supervisor-loss",
        ),
        "orphan-digest-reserved": (
            "after-digest-reservation",
            "999995",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
            ),
            "supervisor-loss",
        ),
        "orphan-child-result": (
            "after-child-result",
            "999996",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
                "child-result.json",
            ),
            "supervisor-loss",
        ),
    }
    if case in orphan_points:
        test_unsealed_v2_interruption_becomes_typed_supervisor_loss(
            tmp_path, *orphan_points[case]
        )
        return
    if case == "partial-envelope":
        test_supervisor_loss_rejects_conflicting_partial_durable_terminal(
            tmp_path, "terminal-envelope.json"
        )
        return
    if case == "partial-digest":
        test_supervisor_loss_rejects_conflicting_partial_durable_terminal(
            tmp_path, "terminal-envelope.sha256"
        )
        return
    if case == "repeated-reconcile":
        test_supervisor_loss_reconciliation_is_idempotent(tmp_path)
        return
    if case == "live-lease-held":
        test_reconcile_refuses_while_the_lifecycle_lease_is_held(tmp_path)
        return
    if case == "prebinding-replacement":
        test_start_replaces_only_a_pre_binding_abandoned_directory(tmp_path)
        return
    if case == "nonforced-cleanup":
        test_nonforced_cleanup_preserves_an_unreconciled_job(tmp_path)
        return
    if case == "forced-cleanup":
        test_forced_cleanup_waits_for_lease_release_before_removing(tmp_path)
        return
    raise AssertionError(f"unhandled interruption pilot case: {case}")


@pytest.mark.parametrize(
    ("window", "pid", "temporary_names", "reason"),
    [
        ("after-binding-before-pid", None, (), "startup-failure"),
        ("after-pid-publication", "999991", (), "supervisor-loss"),
        ("dead-pid", "999992", (), "supervisor-loss"),
        ("reused-live-pid", "current", (), "supervisor-loss"),
        ("after-exit-reservation", "999993", ("exit_code.tmp",), "supervisor-loss"),
        (
            "after-envelope-reservation",
            "999994",
            ("exit_code.tmp", "terminal-envelope.json.tmp"),
            "supervisor-loss",
        ),
        (
            "after-digest-reservation",
            "999995",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
            ),
            "supervisor-loss",
        ),
        (
            "after-child-result",
            "999996",
            (
                "exit_code.tmp",
                "terminal-envelope.json.tmp",
                "terminal-envelope.sha256.tmp",
                "child-result.json",
            ),
            "supervisor-loss",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_unsealed_v2_interruption_becomes_typed_supervisor_loss(
    tmp_path: Path,
    window: str,
    pid: str | None,
    temporary_names: tuple[str, ...],
    reason: str,
) -> None:
    del window
    resolved_pid = str(os.getpid()) if pid == "current" else pid
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid=resolved_pid)
    for temporary_name in temporary_names:
        (job_dir / temporary_name).write_text("forensic-only\n", encoding="utf-8")

    reconciled = _job(tmp_path, "reconcile", binding["name"])

    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(reconciled.stdout)
    assert receipt == {
        "schema_version": "job-supervisor-loss-v1",
        "name": binding["name"],
        "run_id": binding["run_id"],
        "task_id": binding["task_id"],
        "failure_class": "supervisor-loss",
        "reason": reason,
        "binding_sha256": hashlib.sha256(
            (job_dir / "binding.json").read_bytes()
        ).hexdigest(),
    }
    assert "terminal_status" not in receipt
    assert "terminal_artifact" not in receipt
    assert not (job_dir / "exit_code").exists()
    assert _job(tmp_path, "check").returncode == 0


@pytest.mark.parametrize(
    "durable_name", ["terminal-envelope.json", "terminal-envelope.sha256"]
)
def test_supervisor_loss_rejects_conflicting_partial_durable_terminal(
    tmp_path: Path, durable_name: str
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999997")
    (job_dir / durable_name).write_text("partial\n", encoding="utf-8")

    reconciled = _job(tmp_path, "reconcile", binding["name"])

    assert reconciled.returncode != 0
    assert "conflicting partial durable" in reconciled.stderr
    assert not (job_dir / "reconciliation.json").exists()


@pytest.mark.parametrize(
    "durable_name", ["terminal-envelope.json", "terminal-envelope.sha256"]
)
def test_supervisor_loss_rejects_dangling_durable_terminal_symlink(
    tmp_path: Path, durable_name: str
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999997")
    (job_dir / durable_name).symlink_to(job_dir / "missing-terminal-artifact")

    reconciled = _job(tmp_path, "reconcile", binding["name"])

    assert reconciled.returncode != 0
    assert "durable terminal files cannot be symlinks" in reconciled.stderr
    assert not (job_dir / "reconciliation.json").exists()


def test_supervisor_loss_reconciliation_is_idempotent(tmp_path: Path) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")

    first = _job(tmp_path, "reconcile", binding["name"])
    second = _job(tmp_path, "reconcile", binding["name"])

    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert json.loads((job_dir / "reconciliation.json").read_text()) == json.loads(
        first.stdout
    )


def test_supervisor_loss_rejects_a_conflicting_existing_receipt(tmp_path: Path) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999999")
    (job_dir / "reconciliation.json").write_text("{}\n", encoding="utf-8")

    reconciled = _job(tmp_path, "reconcile", binding["name"])

    assert reconciled.returncode != 0
    assert "conflicts with supervisor loss" in reconciled.stderr
    assert _job(tmp_path, "check").returncode == 1
    assert _job(tmp_path, "start", binding["name"], "--", "true").returncode == 2
    assert (job_dir / "binding.json").exists()


def _lease_path(tmp_path: Path, name: str) -> Path:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/util/job_store.py"),
            "--worktree",
            str(REPO_ROOT),
            "--ensure-job-lease",
            name,
        ],
        env={"INTELFLO_JOB_DIR": str(tmp_path / "jobs")},
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(completed.stdout.strip())


def _hold_lease(path: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys,time; "
                "fd=os.open(sys.argv[1], os.O_RDWR); "
                "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); "
                "time.sleep(30)"
            ),
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_reconcile_refuses_while_the_lifecycle_lease_is_held(tmp_path: Path) -> None:
    _, binding = _write_orphaned_v2_binding(tmp_path, pid=str(os.getpid()))
    holder = _hold_lease(_lease_path(tmp_path, binding["name"]))
    try:
        reconciled = _job(tmp_path, "reconcile", binding["name"])
        assert reconciled.returncode == 2
        assert "active launcher or supervisor" in reconciled.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_reconcile_unknown_job_does_not_create_a_stable_lease(
    tmp_path: Path,
) -> None:
    reconciled = _job(tmp_path, "reconcile", "unknown-job")

    assert reconciled.returncode == 2
    assert "no such job" in reconciled.stderr
    assert not (tmp_path / "jobs" / ".leases" / "unknown-job.lock").exists()


def test_start_replaces_only_a_pre_binding_abandoned_directory(tmp_path: Path) -> None:
    abandoned = tmp_path / "jobs" / "pre-binding"
    abandoned.mkdir(parents=True)
    (abandoned / "cmd").write_text("old\n", encoding="utf-8")

    started = _job(tmp_path, "run", "pre-binding", "--timeout", "30", "--", "true")

    assert started.returncode == 0, started.stderr
    assert (abandoned / "exit_code").read_text(encoding="ascii").strip() == "0"


def test_start_preserves_an_unreconciled_visible_binding(tmp_path: Path) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999991")

    started = _job(tmp_path, "start", binding["name"], "--", "true")

    assert started.returncode == 2
    assert "unreaped result" in started.stderr
    assert (job_dir / "binding.json").exists()


def test_nonforced_cleanup_preserves_an_unreconciled_job(tmp_path: Path) -> None:
    job_dir, _ = _write_orphaned_v2_binding(tmp_path, pid="999991")

    cleaned = _job(tmp_path, "clean")

    assert cleaned.returncode == 0
    assert job_dir.exists()


def test_forced_cleanup_waits_for_lease_release_before_removing(tmp_path: Path) -> None:
    assert _job(tmp_path, "start", "forced", "--", "sleep", "30").returncode == 0

    cleaned = _job(tmp_path, "clean", "--all", timeout=20)

    assert cleaned.returncode == 0, cleaned.stderr
    assert not (tmp_path / "jobs" / "forced").exists()


def test_forced_cleanup_refuses_a_live_job_when_the_lifecycle_lease_is_missing(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(["sleep", "30"])
    job_dir = tmp_path / "jobs" / "legacy-live"
    job_dir.mkdir(parents=True)
    (job_dir / "pid").write_text(f"{process.pid}\n", encoding="ascii")
    try:
        cleaned = _job(tmp_path, "clean", "--all")

        assert cleaned.returncode == 2
        assert "lifecycle lease is missing" in cleaned.stderr
        assert process.poll() is None
        assert job_dir.exists()
        assert not (tmp_path / "jobs" / ".leases" / "legacy-live.lock").exists()
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("recorded_pid", ["0", "-1", "not-a-pid"])
def test_forced_cleanup_refuses_an_invalid_recorded_pid(
    tmp_path: Path, recorded_pid: str
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid=recorded_pid)
    holder = _hold_lease(_lease_path(tmp_path, binding["name"]))
    try:
        cleaned = _job(tmp_path, "clean", "--all")

        assert cleaned.returncode == 2
        assert "cannot authenticate" in cleaned.stderr
        assert holder.poll() is None
        assert job_dir.exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_forced_cleanup_continues_after_an_unauthenticated_job(
    tmp_path: Path,
) -> None:
    blocked_dir, blocked_binding = _write_orphaned_v2_binding(
        tmp_path, name="a-blocked", pid="not-a-pid"
    )
    holder = _hold_lease(_lease_path(tmp_path, blocked_binding["name"]))
    try:
        completed = _job(tmp_path, "run", "z-completed", "--", "true")
        assert completed.returncode == 0, completed.stderr

        cleaned = _job(tmp_path, "clean", "--all")

        assert cleaned.returncode == 2
        assert "cannot authenticate" in cleaned.stderr
        assert blocked_dir.exists()
        assert not (tmp_path / "jobs" / "z-completed").exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_forced_cleanup_refuses_a_pid_that_does_not_hold_the_lease(
    tmp_path: Path,
) -> None:
    unrelated = subprocess.Popen(["sleep", "30"])
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid=str(unrelated.pid))
    holder = _hold_lease(_lease_path(tmp_path, binding["name"]))
    try:
        cleaned = _job(tmp_path, "clean", "--all")

        assert cleaned.returncode == 2
        assert "cannot authenticate" in cleaned.stderr
        assert unrelated.poll() is None
        assert holder.poll() is None
        assert job_dir.exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_child_descendant_cannot_retain_the_lifecycle_lease(tmp_path: Path) -> None:
    assert (
        _job(tmp_path, "start", "descendant", "--", "sh", "-c", "sleep 5 &").returncode
        == 0
    )
    _wait_until_done(tmp_path, "descendant")
    assert _job(tmp_path, "wait", "descendant", "--timeout", "30").returncode == 0

    replacement = _job(tmp_path, "run", "descendant", "--timeout", "30", "--", "true")

    assert replacement.returncode == 0, replacement.stderr


def test_cleanup_never_replaces_the_stable_per_name_lease(tmp_path: Path) -> None:
    assert (
        _job(tmp_path, "run", "stable", "--timeout", "30", "--", "true").returncode == 0
    )
    lease = _lease_path(tmp_path, "stable")
    before = lease.stat()

    assert _job(tmp_path, "clean", "--all").returncode == 0
    assert (
        _job(tmp_path, "run", "stable", "--timeout", "30", "--", "true").returncode == 0
    )
    after = lease.stat()

    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_start_fails_before_binding_when_lock_exclusion_is_unproven(
    tmp_path: Path,
) -> None:
    script = _isolated_job_script(tmp_path)
    source = script.read_text(encoding="utf-8")
    source = source.replace(
        'if ! prove_job_lease_exclusion "$lease_path"; then',
        "if true; then # instrument unsupported lock semantics",
        1,
    )
    script.write_text(source, encoding="utf-8")

    started = _job(tmp_path, "start", "unsupported", "--", "true", script=script)

    assert started.returncode == 2
    assert "cannot prove independent lifecycle-lock exclusion" in started.stderr
    assert not (tmp_path / "jobs" / "unsupported" / "binding.json").exists()


@requires_nested_user_namespace
def test_seal_wins_before_reconcile_can_acquire_the_lease(tmp_path: Path) -> None:
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "race", "status": "completed"}), encoding="utf-8"
    )
    started = _job(
        tmp_path,
        "start",
        "seal-race",
        "--run-id",
        "sr_" + "b" * 32,
        "--task-id",
        "race",
        "--terminal-artifact",
        str(artifact),
        "--",
        "true",
    )
    assert started.returncode == 0, started.stderr

    early = _job(tmp_path, "reconcile", "seal-race")
    assert early.returncode in {0, 2}
    _wait_until_done(tmp_path, "seal-race")
    final = _job(tmp_path, "reconcile", "seal-race")

    assert final.returncode == 0, final.stderr
    assert json.loads(final.stdout)["schema_version"] == "job-reconciliation-v2"
