
from .package_environment import package_environment
import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    return importlib.import_module('loopzero.kernel.liveness')


module = load_module()
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def run_entry(
    *, run_id: str, outcome: str, started_at: datetime, branch: str
) -> dict[str, object]:
    return {
        "ts": started_at.isoformat().replace("+00:00", "Z"),
        "run_id": run_id,
        "skill": "work-issue",
        "outcome": outcome,
        "git_branch": branch,
    }


def test_stalled_run_and_long_run_use_shared_branch_events() -> None:
    run_id = "sr_" + "a" * 32
    started_at = NOW - timedelta(hours=25)
    entries = [
        run_entry(
            run_id=run_id,
            outcome="in_progress",
            started_at=started_at,
            branch="feature/stalled",
        )
    ]
    events = [
        {
            "ts": (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
            "git_branch": "feature/stalled",
            "kind": "phase",
        }
    ]

    report = module.build_report(entries, events, (), now=NOW)

    assert len(report.stalled_runs) == 1
    assert report.stalled_runs[0].last_event_at == NOW - timedelta(hours=3)
    assert len(report.long_running) == 1
    assert report.long_running[0].recommendation == "re-entry capsule rollover"
    rendered = module.render(report)
    assert "feature/stalled" in rendered
    assert "re-entry capsule rollover" in rendered


def test_recent_branch_event_prevents_stalled_run_finding() -> None:
    started_at = NOW - timedelta(hours=5)
    entries = [
        run_entry(
            run_id="sr_" + "b" * 32,
            outcome="in_progress",
            started_at=started_at,
            branch="feature/active",
        )
    ]
    events = [
        {
            "ts": (NOW - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "git_branch": "feature/active",
        }
    ]

    report = module.build_report(entries, events, (), now=NOW)

    assert report.stalled_runs == ()
    assert report.long_running == ()


def test_empty_report_says_it_is_not_active_work_inventory() -> None:
    report = module.build_report((), (), (), now=NOW)

    rendered = module.render(report)

    assert "no anomalies" in rendered
    assert "not active-work inventory" in rendered


def test_legacy_in_progress_row_without_run_id_is_not_actionable() -> None:
    entries = [
        {
            "ts": (NOW - timedelta(hours=25)).isoformat().replace("+00:00", "Z"),
            "skill": "execute-blueprint",
            "outcome": "in_progress",
            "git_branch": "blueprint/already-finished-legacy-work",
        }
    ]

    report = module.build_report(entries, (), (), now=NOW)

    assert report.stalled_runs == ()
    assert report.long_running == ()


def test_terminal_state_is_selected_by_timestamp_not_input_order() -> None:
    run_id = "sr_" + "c" * 32
    entries = [
        run_entry(
            run_id=run_id,
            outcome="merged",
            started_at=NOW - timedelta(hours=1),
            branch="feature/completed",
        ),
        run_entry(
            run_id=run_id,
            outcome="in_progress",
            started_at=NOW - timedelta(hours=5),
            branch="feature/completed",
        ),
    ]

    report = module.build_report(entries, (), (), now=NOW)

    assert report.stalled_runs == ()
    assert report.long_running == ()


def test_aged_dirty_worktree_uses_latest_dirty_path_or_index_activity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worktree"
    path.mkdir()
    dirty_file = path / "changed.py"
    dirty_file.write_text("changed", encoding="utf-8")
    old = (NOW - timedelta(hours=3)).timestamp()
    os.utime(dirty_file, (old, old))
    worktree = module.RegisteredWorktree(path, "feature/old-dirty")
    finding = module.AgedDirtyWorktree(
        worktree=worktree,
        dirty_paths=("changed.py",),
        latest_activity_at=NOW - timedelta(hours=3),
        idle_for=timedelta(hours=3),
    )

    report = module.build_report(
        (),
        (),
        (worktree,),
        now=NOW,
        inspector=lambda _worktree, _now: finding,
    )

    assert len(report.aged_dirty_worktrees) == 1
    assert report.aged_dirty_worktrees[0].worktree.branch == "feature/old-dirty"
    assert "feature/old-dirty" in module.render(report)


def test_dirty_worktree_inspection_uses_index_mtime_and_status_paths(
    tmp_path: Path, monkeypatch
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    index = worktree_path / "index"
    index.write_text("index", encoding="utf-8")
    dirty_file = worktree_path / "changed.py"
    dirty_file.write_text("changed", encoding="utf-8")
    old = (NOW - timedelta(hours=3)).timestamp()
    os.utime(index, (old, old))
    os.utime(dirty_file, (old, old))
    worktree = module.RegisteredWorktree(worktree_path, "feature/old-dirty")
    monkeypatch.setattr(module, "_worktree_index_path", lambda _path: index)
    monkeypatch.setattr(module, "_status_paths", lambda _path: ("changed.py",))

    finding = module.inspect_dirty_worktree(worktree, NOW)

    assert finding is not None
    assert finding.latest_activity_at == NOW - timedelta(hours=3)
    assert finding.dirty_paths == ("changed.py",)


def test_fresh_unstaged_deletion_uses_parent_directory_activity(
    tmp_path: Path, monkeypatch
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    index = worktree_path / "index"
    index.write_text("index", encoding="utf-8")
    old = (NOW - timedelta(hours=3)).timestamp()
    recent = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(index, (old, old))
    os.utime(worktree_path, (recent, recent))
    worktree = module.RegisteredWorktree(worktree_path, "feature/fresh-delete")
    monkeypatch.setattr(module, "_worktree_index_path", lambda _path: index)
    monkeypatch.setattr(module, "_status_paths", lambda _path: ("deleted.py",))

    assert module.inspect_dirty_worktree(worktree, NOW) is None


@pytest.mark.parametrize(
    ("link_age", "target_age", "expected_latest_activity"),
    [
        ("fresh", "old", NOW - timedelta(minutes=5)),
        ("old", "fresh", NOW - timedelta(hours=3)),
    ],
)
def test_symlink_activity_uses_link_mtime_not_target_mtime(
    tmp_path: Path,
    monkeypatch,
    link_age: str,
    target_age: str,
    expected_latest_activity: datetime,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    index = worktree_path / "index"
    index.write_text("index", encoding="utf-8")
    target = worktree_path / "target.py"
    target.write_text("target", encoding="utf-8")
    link = worktree_path / "changed.py"
    link.symlink_to(target)

    old = (NOW - timedelta(hours=3)).timestamp()
    fresh = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(index, (old, old))
    os.utime(target, (old if target_age == "old" else fresh,) * 2)
    os.utime(link, (old if link_age == "old" else fresh,) * 2, follow_symlinks=False)

    worktree = module.RegisteredWorktree(worktree_path, "feature/symlink-mtime")
    monkeypatch.setattr(module, "_worktree_index_path", lambda _path: index)
    monkeypatch.setattr(module, "_status_paths", lambda _path: ("changed.py",))

    finding = module.inspect_dirty_worktree(worktree, NOW)

    if link_age == "fresh":
        assert finding is None
    else:
        assert finding is not None
        assert finding.latest_activity_at == expected_latest_activity


def test_path_activity_does_not_walk_outside_worktree(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    assert module._path_activity_mtime(tmp_path, worktree_path) is None


def test_registered_worktree_porcelain_parser_handles_detached_entries() -> None:
    entries = module.parse_registered_worktrees(
        "\n".join(
            [
                "worktree /repo/main",
                "HEAD abc",
                "branch refs/heads/main",
                "",
                "worktree /repo/feature",
                "HEAD def",
                "detached",
                "",
            ]
        )
    )

    assert entries == (
        module.RegisteredWorktree(Path("/repo/main"), "main"),
        module.RegisteredWorktree(Path("/repo/feature"), "(detached)"),
    )


def test_stalled_worktree_status_fails_closed_and_names_resource(
    tmp_path: Path, monkeypatch
) -> None:
    worktree = tmp_path / "stalled-worktree"
    worktree.mkdir()

    def stall(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", stall)

    with pytest.raises(module.CollectorError, match="worktree-status") as exc_info:
        module._status_paths(worktree)

    assert str(worktree) in str(exc_info.value)
    assert f"{module.GIT_COMMAND_TIMEOUT_SECONDS}s" in str(exc_info.value)


def test_failed_worktree_status_is_not_reported_as_clean(
    tmp_path: Path, monkeypatch
) -> None:
    worktree = tmp_path / "failed-worktree"
    worktree.mkdir()
    completed = subprocess.CompletedProcess(
        args=["git", "status"], returncode=128, stdout="", stderr="not a repository"
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(module.CollectorError, match="worktree-status") as exc_info:
        module._status_paths(worktree)

    assert "exit 128" in str(exc_info.value)


def test_unstartable_git_is_a_typed_collector_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("git is unavailable")
        ),
    )

    with pytest.raises(module.CollectorError, match="git-common-dir could not start"):
        module.repo_root()


def test_main_returns_nonzero_and_names_failed_collector(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        module,
        "collect_report",
        lambda: (_ for _ in ()).throw(
            module.CollectorError("worktree-status [/repo/stalled] timed out after 10s")
        ),
    )

    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "worktree-status [/repo/stalled] timed out after 10s" in captured.err


def test_main_renders_normal_completion(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        module,
        "collect_report",
        lambda: module.LivenessReport((), (), ()),
    )

    assert module.main() == 0
    captured = capsys.readouterr()
    assert "no anomalies — exception report, not active-work inventory" in captured.out
    assert captured.err == ""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _preflight_environment(
    tmp_path: Path,
    *,
    make_body: str,
    gh_body: str = "exit 0",
    bash_body: str = "exit 0",
    python_body: str = "exit 0",
) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "gh", gh_body)
    _write_executable(fake_bin / "make", make_body)
    _write_executable(fake_bin / "bash", bash_body)
    _write_executable(fake_bin / "python3", python_body)
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": "/tmp",
        "TMP": "/tmp",
        "TEMP": "/tmp",
    }


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_completes_when_collectors_respond(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(_preflight_environment(tmp_path, make_body="exit 0")),
    )

    assert result.returncode == 0
    assert "== review provider authentication ==" in result.stdout
    assert "== local worktree ownership ==" in result.stdout
    assert "preflight: OK (#3202)" in result.stdout


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_checks_opus_auth_once_at_intake(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    call_log = tmp_path / "python-calls"
    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(_preflight_environment(
            tmp_path,
            make_body="exit 0",
            python_body=f'printf "%s\\n" "$*" >> "{call_log}"; exit 0',
        )),
    )

    assert result.returncode == 0
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        f"{repo_root}/scripts/util/agent_dispatch_host.py doctor --alias opus "
        "--timeout-s 900"
    ]


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_gives_provider_refresh_and_cleanup_a_longer_bound(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    timeout_log = tmp_path / "timeout-calls"
    environment = _preflight_environment(tmp_path, make_body="exit 0")
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "timeout",
        f'printf "%s\\n" "$3" >> "{timeout_log}"; shift 3; exec "$@"',
    )

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(environment),
    )

    assert result.returncode == 0
    assert timeout_log.read_text(encoding="utf-8").splitlines()[:2] == [
        "30s",
        "120s",
    ]


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_auth_failure_requests_one_intake_login(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(_preflight_environment(
            tmp_path,
            make_body="exit 0",
            python_body=(
                "echo 'cursor: authentication check failed; "
                "run cursor-agent login'; exit 1"
            ),
        )),
    )

    assert result.returncode == 1
    assert result.stdout.count("cursor-agent login") == 1
    assert "claude auth login" not in result.stderr
    assert "follow any provider login guidance above" in result.stderr
    assert "== lanes (remote coordination gate) ==" not in result.stdout


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_provider_timeout_does_not_claim_login_guidance_was_reported(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    environment = _preflight_environment(
        tmp_path, make_body="exit 0", python_body="sleep 2"
    )
    environment["PREFLIGHT_REVIEW_AUTH_TIMEOUT_SECONDS"] = "0.1"

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(environment),
    )

    assert result.returncode == 1
    assert "review provider authentication timed out after 0.1s" in result.stderr
    assert "provider login reported above" not in result.stderr


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_fails_before_auth_when_ripgrep_is_unavailable(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    environment = _preflight_environment(tmp_path, make_body="exit 9", gh_body="exit 9")
    fake_bin = tmp_path / "bin"
    _write_executable(fake_bin / "timeout", 'shift 3; exec "$@"')
    environment["PATH"] = str(fake_bin)

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(environment),
    )

    assert result.returncode == 1
    assert (
        "preflight: ripgrep (rg) is required but unavailable on PATH" in result.stderr
    )
    assert "GitHub auth unavailable" not in result.stderr
    assert result.stdout == ""


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_stalled_lanes_collector_exits_within_bound_and_names_it(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    environment = _preflight_environment(tmp_path, make_body="sleep 2")
    environment["PREFLIGHT_STEP_TIMEOUT_SECONDS"] = "0.1"

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(environment),
    )

    assert result.returncode == 1
    assert "preflight: lanes collector timed out after 0.1s" in result.stderr
    assert "== collision guard ==" not in result.stdout
    assert "preflight: OK" not in result.stdout


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_preserves_collision_guard_exit_three(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(_preflight_environment(tmp_path, make_body="exit 0", bash_body="exit 3")),
    )

    assert result.returncode == 3
    assert "preflight: collision guard failed with exit 3" in result.stderr
    assert "preflight: OK" not in result.stdout


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_collision_timeout_is_failure_not_collision(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    environment = _preflight_environment(
        tmp_path, make_body="exit 0", bash_body="sleep 2"
    )
    environment["PREFLIGHT_STEP_TIMEOUT_SECONDS"] = "0.1"

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(environment),
    )

    assert result.returncode == 1
    assert "preflight: collision guard timed out after 0.1s" in result.stderr


@pytest.mark.skip(reason='Consumer preflight.sh stays in intelflo; TODO(A4) integration lane')
def test_preflight_quiet_auth_timeout_keeps_wrapper_diagnostic(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    environment = _preflight_environment(
        tmp_path, make_body="exit 0", gh_body="sleep 2"
    )
    environment["PREFLIGHT_STEP_TIMEOUT_SECONDS"] = "0.1"

    result = subprocess.run(
        [
            "/bin/bash",
            str(repo_root / "scripts/util/preflight.sh"),
            "3202",
            "--no-label",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=package_environment(environment),
    )

    assert result.returncode == 1
    assert "preflight: GitHub auth timed out after 0.1s" in result.stderr
    assert "GitHub auth unavailable" in result.stderr
