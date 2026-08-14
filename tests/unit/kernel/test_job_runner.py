"""Coverage for the detached long-running-command runner used by agents."""

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "util" / "job.sh"


def _job(
    tmp_path: Path, *args: str, timeout: float = 60, script: Path = SCRIPT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=script.resolve().parents[2],
        env={"PATH": "/usr/bin:/bin", "INTELFLO_JOB_DIR": str(tmp_path / "jobs")},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


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
    for _ in range(50):
        if _job(tmp_path, "check").returncode != 0:
            break

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

    reconciled = _job(
        tmp_path,
        "reconcile",
        "bound",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 0, reconciled.stderr
    receipt = json.loads(
        (tmp_path / "jobs" / "bound" / "reconciliation.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["terminal_artifact_sha256"]
    assert receipt["terminal_status"] == "completed"
    assert "terminal_authority" not in receipt
    assert _job(tmp_path, "check").returncode == 0
    bindings = _job(tmp_path, "binding-files", "--run-id", run_id)
    assert bindings.stdout.strip() == str(tmp_path / "jobs" / "bound" / "binding.json")
    assert (
        _job(
            tmp_path,
            "reconcile",
            "bound",
            "--terminal-artifact",
            str(artifact),
        ).returncode
        == 0
    )


@pytest.mark.parametrize("runtime_contract_version", [4, None])
def test_bound_job_reconciles_legacy_blocked_dispatch_from_terminal_telemetry(
    tmp_path: Path,
    runtime_contract_version: int | None,
) -> None:
    run_id = "sr_" + "a" * 32
    task_id = "dispatch-legacy-blocked"
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

    reconciled = _job(
        tmp_path,
        "reconcile",
        "bound",
        "--terminal-artifact",
        str(artifact),
        script=script,
    )

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


def test_bound_job_rejects_legacy_blocked_result_without_terminal_telemetry(
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

    reconciled = _job(
        tmp_path,
        "reconcile",
        "bound",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 1
    assert "dispatcher terminal telemetry" in reconciled.stderr


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
        "--terminal-artifact",
        str(artifact),
        script=script,
    )

    assert reconciled.returncode == 1
    assert "dispatcher terminal telemetry" in reconciled.stderr


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

    reconciled = _job(
        tmp_path,
        "reconcile",
        "bound",
        "--terminal-artifact",
        str(artifact),
    )

    assert reconciled.returncode == 1
    assert "task_id" in reconciled.stderr
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

    reconciled = _job(
        tmp_path,
        "reconcile",
        "bound",
        "--terminal-artifact",
        str(artifact),
    )

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
