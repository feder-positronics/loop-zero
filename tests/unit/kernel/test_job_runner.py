"""Coverage for the detached long-running-command runner used by agents."""

import hashlib
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
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured = tmp_path / "job-store-args"
    job_root = tmp_path / "jobs"
    job_root.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {str(captured)!r}\n"
        f"printf '%s\\n' {str(job_root)!r}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    delivery = tmp_path / "delivery"
    delivery.mkdir()

    result = subprocess.run(
        [str(SCRIPT), "list"],
        cwd=delivery,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "INTELFLO_DELIVERY_ROOT": str(delivery),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert captured.read_text(encoding="utf-8").splitlines() == [
        str(REPO_ROOT / "scripts/util/job_store.py"),
        "--worktree",
        str(delivery),
        "--ensure-root",
    ]


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
    attack = (
        f'printf "%s\\n" "$$" > "{job_dir / "pid"}"; '
        f'exec "{SCRIPT}" _execute "{job_dir}" "{forged}" -- true'
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
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return script


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
