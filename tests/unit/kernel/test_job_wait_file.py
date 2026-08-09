"""Subprocess tests for `job.sh wait-file` (#3418 P1 event-driven waits).

The dispatched-worker fixture proves a wait completes via the terminal
artifact with zero model-side wakes (one blocking call); the killed-worker
fixture proves the deadman exit routes to the orphan path.
"""

import subprocess
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
JOB_SH = REPO_ROOT / "scripts" / "util" / "job.sh"


def run_wait_file(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(JOB_SH), "wait-file", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


def test_wait_file_wakes_when_artifact_appears(tmp_path: Path) -> None:
    artifact = tmp_path / "handoff" / "terminal.json"
    artifact.parent.mkdir()

    def worker() -> None:
        time.sleep(3)
        artifact.write_text('{"mode": "terminal", "status": "done"}\n')

    thread = threading.Thread(target=worker)
    started = time.monotonic()
    thread.start()
    result = run_wait_file(
        [str(artifact), "--timeout", "600", "--fast-cadence", "test fixture"],
        tmp_path,
    )
    elapsed = time.monotonic() - started
    thread.join()

    assert result.returncode == 0, result.stderr
    # Woke shortly after the artifact appeared — event, not timeout.
    assert elapsed < 30
    assert "terminal.json" in result.stdout
    assert '"mode": "terminal"' in result.stdout


def test_wait_file_deadman_routes_to_orphan_path(tmp_path: Path) -> None:
    missing = tmp_path / "never-written.json"
    result = run_wait_file(
        [str(missing), "--timeout", "2", "--fast-cadence", "test fixture"],
        tmp_path,
    )
    assert result.returncode == 124
    assert "presumed dead" in result.stderr
    assert "orphan" in result.stderr
    assert "do NOT re-wait" in result.stderr


def test_wait_file_preexisting_artifact_returns_immediately(tmp_path: Path) -> None:
    artifact = tmp_path / "already-there.json"
    artifact.write_text("{}\n")
    started = time.monotonic()
    result = run_wait_file(
        [str(artifact), "--timeout", "600", "--fast-cadence", "test fixture"],
        tmp_path,
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 10


def test_wait_file_short_timeout_warns_without_fast_cadence(tmp_path: Path) -> None:
    artifact = tmp_path / "quick.json"
    artifact.write_text("{}\n")
    result = run_wait_file([str(artifact), "--timeout", "30"], tmp_path)
    assert result.returncode == 0
    assert "below the 300s wait floor" in result.stderr

    quiet = run_wait_file(
        [str(artifact), "--timeout", "30", "--fast-cadence", "state flips in 5s"],
        tmp_path,
    )
    assert quiet.returncode == 0
    assert "wait floor" not in quiet.stderr


def test_wait_short_timeout_warns_on_job_wait_too(tmp_path: Path) -> None:
    env_dir = tmp_path / "jobs"
    subprocess.run(
        ["bash", str(JOB_SH), "start", "quick", "--", "true"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "INTELFLO_JOB_DIR": str(env_dir)},
        timeout=30,
    )
    result = subprocess.run(
        ["bash", str(JOB_SH), "wait", "quick", "--timeout", "60"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "INTELFLO_JOB_DIR": str(env_dir)},
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert "below the 300s wait floor" in result.stderr
