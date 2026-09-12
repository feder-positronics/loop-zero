import importlib.util
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    return importlib.import_module('loopzero.kernel.worktree_claims')


module = load_module()


def worktree(path: str, branch: str = "feature/example"):
    return module.RegisteredWorktree(Path(path), branch)


def test_parse_registered_worktrees_preserves_detached_and_missing_entries() -> None:
    entries = module.parse_registered_worktrees(
        "\n".join(
            [
                "worktree /repo/main",
                "HEAD abc",
                "branch refs/heads/main",
                "",
                "worktree /repo/missing",
                "HEAD def",
                "detached",
                "prunable gitdir file points to non-existent location",
            ]
        )
    )

    assert entries == (
        module.RegisteredWorktree(Path("/repo/main"), "main"),
        module.RegisteredWorktree(Path("/repo/missing"), "(detached)"),
    )


def test_assign_processes_uses_longest_registered_worktree_prefix() -> None:
    registered = (
        worktree("/repo", "main"),
        worktree("/repo/.worktrees/child", "feature/child"),
    )

    assignments = module.assign_processes_to_worktrees(
        registered,
        {
            10: Path("/repo/app"),
            11: Path("/repo/.worktrees/child/scripts"),
            12: Path("/elsewhere"),
        },
    )

    assert assignments == {
        Path("/repo"): (10,),
        Path("/repo/.worktrees/child"): (11,),
    }


def test_dirty_paths_parses_porcelain_records(monkeypatch) -> None:
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=" M changed.py\0?? new.py\0", stderr=""
    )
    monkeypatch.setattr(module, "_run_git", lambda *args, **kwargs: result)

    assert module.dirty_paths(Path("/repo")) == ("changed.py", "new.py")


def test_collect_dirty_paths_runs_worktree_probes_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    barrier = threading.Barrier(2, timeout=2)

    def probe(path: Path) -> tuple[str, ...]:
        barrier.wait()
        return (f"{path.name}.py",)

    monkeypatch.setattr(module, "dirty_paths", probe)

    statuses = module.collect_dirty_paths((worktree(str(first)), worktree(str(second))))

    assert statuses == {
        first: ("first.py",),
        second: ("second.py",),
    }


def test_scan_process_cwds_reads_only_numeric_cwd_links(tmp_path: Path) -> None:
    target = tmp_path / "worktree"
    target.mkdir()
    process = tmp_path / "123"
    process.mkdir()
    (process / "cwd").symlink_to(target, target_is_directory=True)
    (tmp_path / "not-a-pid").mkdir()

    probe = module.scan_process_cwds(tmp_path)

    assert probe.available is True
    assert probe.cwd_by_pid == {123: target}


def test_classify_current_worktree_takes_precedence_over_occupancy() -> None:
    claim = module.classify_worktree(
        worktree("/repo/current", "feature/current"),
        current_worktree=Path("/repo/current"),
        path_exists=True,
        dirty_paths=("changed.py",),
        process_ids=(101, 102),
        process_probe_available=True,
    )

    assert claim.state == module.ClaimState.CURRENT
    assert claim.dirty_count == 1
    assert claim.process_count == 2


def test_classify_live_process_as_owned_even_when_tree_is_fresh_and_dirty() -> None:
    claim = module.classify_worktree(
        worktree("/repo/live"),
        current_worktree=Path("/repo/current"),
        path_exists=True,
        dirty_paths=("new-untracked.py",),
        process_ids=(101,),
        process_probe_available=True,
    )

    assert claim.state == module.ClaimState.OWNED_LIVE
    assert claim.dirty_count == 1
    assert claim.provenance == ("git-worktree", "git-status", "process-cwd")


def test_classify_parked_dirty_and_idle_clean_worktrees() -> None:
    parked = module.classify_worktree(
        worktree("/repo/parked"),
        current_worktree=Path("/repo/current"),
        path_exists=True,
        dirty_paths=("changed.py", "new.py"),
        process_ids=(),
        process_probe_available=True,
    )
    idle = module.classify_worktree(
        worktree("/repo/idle"),
        current_worktree=Path("/repo/current"),
        path_exists=True,
        dirty_paths=(),
        process_ids=(),
        process_probe_available=True,
    )

    assert parked.state == module.ClaimState.PARKED_DIRTY
    assert idle.state == module.ClaimState.IDLE_CLEAN


def test_classify_missing_and_probe_unknown_states_fail_visibly() -> None:
    missing = module.classify_worktree(
        worktree("/repo/missing"),
        current_worktree=Path("/repo/current"),
        path_exists=False,
        dirty_paths=None,
        process_ids=(),
        process_probe_available=True,
    )
    unknown = module.classify_worktree(
        worktree("/repo/unknown"),
        current_worktree=Path("/repo/current"),
        path_exists=True,
        dirty_paths=("changed.py",),
        process_ids=(),
        process_probe_available=False,
    )

    assert missing.state == module.ClaimState.ATTENTION
    assert missing.provenance == ("git-worktree", "path:missing", "process-cwd")
    assert unknown.state == module.ClaimState.UNKNOWN
    assert "process-cwd:unavailable" in unknown.provenance


def test_render_names_local_gate_and_distinguishes_current_from_collisions() -> None:
    claims = (
        module.WorktreeClaim(
            Path("/repo/current"),
            "feature/current",
            module.ClaimState.CURRENT,
            0,
            2,
            ("git-worktree", "process-cwd"),
        ),
        module.WorktreeClaim(
            Path("/repo/live"),
            "feature/live",
            module.ClaimState.OWNED_LIVE,
            3,
            1,
            ("git-worktree", "process-cwd"),
        ),
    )

    rendered = module.render_claims(claims, display_root=Path("/repo/current"))

    assert "Local worktree ownership gate" in rendered
    assert "CURRENT" in rendered
    assert "OWNED-LIVE" in rendered
    assert "remote gate" in rendered
    assert "stop on OWNED-LIVE" in rendered
    assert "exception report" not in rendered


def test_render_empty_inventory_is_explicit() -> None:
    rendered = module.render_claims((), display_root=Path("/repo"))

    assert "no registered worktrees" in rendered


def test_issue_claims_use_active_run_and_registered_worktree_as_one_authority() -> None:
    now = datetime(2026, 9, 4, 18, 30, tzinfo=UTC)
    claims = (
        module.WorktreeClaim(
            Path("/repo/current"),
            "feature/current",
            module.ClaimState.CURRENT,
            0,
            1,
            ("git-worktree", "process-cwd"),
        ),
        module.WorktreeClaim(
            Path("/repo/other"),
            "feature/other",
            module.ClaimState.IDLE_CLEAN,
            0,
            0,
            ("git-worktree", "git-status", "process-cwd"),
        ),
    )
    entries = [
        {
            "ts": "2026-09-04T18:00:00Z",
            "run_id": "sr_11111111111111111111111111111111",
            "skill": "work-issue",
            "outcome": "in_progress",
            "issue": 4096,
            "git_branch": "feature/other",
        }
    ]

    issue_claims = module.collect_issue_claims(entries, claims)

    assert issue_claims == (
        module.IssueClaim(
            run_id="sr_11111111111111111111111111111111",
            issue=4096,
            skill="work-issue",
            branch="feature/other",
            started_at=datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            worktree=Path("/repo/other"),
            state=module.IssueClaimState.LIVE,
        ),
    )
    rendered, exit_code = module.render_issue_guard(issue_claims, issue=4096, now=now)
    assert exit_code == 3
    assert "sr_11111111111111111111111111111111" in rendered
    assert "/repo/other" in rendered
    assert "age 30m" in rendered


def test_issue_claim_for_current_worktree_is_not_a_collision() -> None:
    worktree_claims = (
        module.WorktreeClaim(
            Path("/repo/current"),
            "feature/current",
            module.ClaimState.CURRENT,
            0,
            1,
            ("git-worktree", "process-cwd"),
        ),
    )
    entries = [
        {
            "ts": "2026-09-04T18:00:00Z",
            "run_id": "sr_22222222222222222222222222222222",
            "skill": "work-issue",
            "outcome": "in_progress",
            "issue": 4096,
            "git_branch": "feature/current",
        }
    ]

    claims = module.collect_issue_claims(entries, worktree_claims)
    rendered, exit_code = module.render_issue_guard(claims, issue=4096)

    assert claims[0].state == module.IssueClaimState.CURRENT
    assert exit_code == 0
    assert rendered == ""


def test_terminal_and_missing_worktree_claims_do_not_block() -> None:
    now = datetime(2026, 9, 4, 18, 30, tzinfo=UTC)
    entries = [
        {
            "ts": "2026-09-04T16:00:00Z",
            "run_id": "sr_33333333333333333333333333333333",
            "skill": "work-issue",
            "outcome": "in_progress",
            "issue": 4096,
            "git_branch": "feature/abandoned",
        },
        {
            "ts": "2026-09-04T16:05:00Z",
            "run_id": "sr_33333333333333333333333333333333",
            "skill": "work-issue",
            "outcome": "abandoned",
            "issue": 4096,
            "git_branch": "feature/abandoned",
        },
        {
            "ts": "2026-09-04T17:00:00Z",
            "run_id": "sr_44444444444444444444444444444444",
            "skill": "work-issue",
            "outcome": "in_progress",
            "issue": 4096,
            "git_branch": "feature/gone",
        },
    ]

    claims = module.collect_issue_claims(entries, ())
    rendered, exit_code = module.render_issue_guard(claims, issue=4096)

    assert [claim.run_id for claim in claims] == ["sr_44444444444444444444444444444444"]
    assert claims[0].state == module.IssueClaimState.STALE
    assert exit_code == 0
    assert "STALE CLAIM" in rendered
    assert "does not block" in rendered
    assert "--outcome abandoned" in rendered
    assert "--git-branch feature/gone" in rendered


def test_issue_claim_uses_timestamp_not_input_order_for_latest_state() -> None:
    run_id = "sr_66666666666666666666666666666666"
    entries = [
        {
            "ts": "2026-09-04T18:05:00Z",
            "run_id": run_id,
            "skill": "work-issue",
            "outcome": "abandoned",
            "issue": 4096,
            "git_branch": "feature/other",
        },
        {
            "ts": "2026-09-04T18:00:00Z",
            "run_id": run_id,
            "skill": "work-issue",
            "outcome": "in_progress",
            "issue": 4096,
            "git_branch": "feature/other",
        },
    ]

    assert module.collect_issue_claims(entries, ()) == ()


def test_render_claims_includes_issue_claim_projection() -> None:
    claim = module.IssueClaim(
        run_id="sr_55555555555555555555555555555555",
        issue=4096,
        skill="work-issue",
        branch="feature/other",
        started_at=datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
        worktree=Path("/repo/other"),
        state=module.IssueClaimState.LIVE,
    )

    rendered = module.render_issue_claims(
        (claim,),
        display_root=Path("/repo/current"),
        now=datetime(2026, 9, 4, 18, 30, tzinfo=UTC),
    )

    assert "Live issue claims (shared run registry)" in rendered
    assert "#4096" in rendered
    assert "LIVE" in rendered
    assert "30m" in rendered


def test_main_renders_issue_claims_from_shared_projection(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    current = tmp_path / "current"
    other = tmp_path / "other"
    current.mkdir()
    other.mkdir()
    worktree_claims = (
        module.WorktreeClaim(
            current,
            "feature/current",
            module.ClaimState.CURRENT,
            0,
            1,
            ("git-worktree", "process-cwd"),
        ),
        module.WorktreeClaim(
            other,
            "feature/other",
            module.ClaimState.IDLE_CLEAN,
            0,
            0,
            ("git-worktree", "git-status", "process-cwd"),
        ),
    )
    entries = [
        {
            "ts": "2026-09-04T18:00:00Z",
            "run_id": "sr_77777777777777777777777777777777",
            "skill": "work-issue",
            "outcome": "in_progress",
            "issue": 4096,
            "git_branch": "feature/other",
        }
    ]
    monkeypatch.setattr(module, "current_worktree", lambda: current)
    monkeypatch.setattr(module, "collect_claims", lambda _root: worktree_claims)
    monkeypatch.setattr(module, "shared_repo_root", lambda _root: tmp_path)
    monkeypatch.setattr(module, "load_skill_runs", lambda _root: entries)
    monkeypatch.setattr(sys, "argv", ["worktree_claims.py"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "#4096" in output
    assert "sr_77777777777777777777777777777777" in output
    assert "LIVE" in output
    assert module._display_path(other, current) in output


def test_main_fails_visibly_when_git_inventory_is_unavailable(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        module,
        "current_worktree",
        lambda: (_ for _ in ()).throw(module.CollectorError("git unavailable")),
    )
    monkeypatch.setattr(sys, "argv", ["worktree_claims.py"])

    assert module.main() == 1
    assert "ownership gate unavailable: git unavailable" in capsys.readouterr().err
