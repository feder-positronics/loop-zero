"""Coverage for the detached long-running-command runner used by agents."""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "util" / "job.sh"


def _job(
    tmp_path: Path, *args: str, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=REPO_ROOT,
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


def test_wait_on_unknown_job_is_a_usage_error(tmp_path: Path) -> None:
    result = _job(tmp_path, "wait", "ghost")

    assert result.returncode == 2
    assert "no such job" in result.stderr
