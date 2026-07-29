import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    module_path = repo_root / "scripts" / "util" / "delivery_liveness.py"
    spec = importlib.util.spec_from_file_location("delivery_liveness", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
