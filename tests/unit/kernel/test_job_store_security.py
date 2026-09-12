"""Security regressions for trusted paths exposed to bound jobs."""

from .package_environment import package_environment

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

from .capabilities import NAMESPACE_AVAILABLE, NAMESPACE_REASON

requires_nested_user_namespace = pytest.mark.skipif(
    not NAMESPACE_AVAILABLE,
    reason=NAMESPACE_REASON,
)


@requires_nested_user_namespace
def test_bound_sandbox_denies_coordinator_ledger_and_global_git_writes(
    tmp_path: Path,
) -> None:
    from loopzero.kernel import jobs as job_store

    home = tmp_path / "home"
    authority = home / ".local/state/example/dispatch-authority"
    job_root = home / ".local/state/example/jobs/repository"
    job_dir = job_root / "current"
    worktree = home / "worktree"
    private_temp = home / "tmp/current"
    global_git = home / ".config/git"
    ssh = home / ".ssh"
    for directory in (
        authority,
        job_dir,
        worktree,
        private_temp,
        global_git,
        ssh,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    coordinator_key = authority / "coordinator-ed25519.pem"
    host_ledger = authority / "ledgers" / ("a" * 64 + ".json")
    dot_gitconfig = home / ".gitconfig"
    config_git = global_git / "config"
    ssh_config = ssh / "config"
    protected = {
        coordinator_key: "coordinator-key\n",
        host_ledger: "ledger-state\n",
        dot_gitconfig: "[user]\n\tname = Coordinator\n",
        config_git: "[core]\n\thooksPath = /dev/null\n",
        ssh_config: "Host *\n\tBatchMode yes\n",
    }
    for path, payload in protected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    arguments = job_store.build_bound_sandbox_arguments(
        "/usr/bin/bwrap",
        protected_authority_root=job_root,
        working_directory=worktree,
        command=[
            "/bin/sh",
            "-c",
            'for target in "$@"; do ! printf attacker > "$target" || exit 9; done; '
            'touch "$JOB_DIR/job-write" "$WORKTREE/worktree-write" "$PRIVATE_TEMP/temp-write"',
            "sh",
            *map(str, protected),
        ],
        account_home=home,
        writable_paths=(job_dir, worktree, private_temp),
        protected_read_only_paths=(),
    )
    result = subprocess.run(
        arguments,
        env={
            "PATH": "/usr/bin:/bin",
            "JOB_DIR": str(job_dir),
            "WORKTREE": str(worktree),
            "PRIVATE_TEMP": str(private_temp),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {path: path.read_text(encoding="utf-8") for path in protected} == protected
    assert (job_dir / "job-write").exists()
    assert (worktree / "worktree-write").exists()
    assert (private_temp / "temp-write").exists()


def _isolated_job_script(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    script = repo / "scripts" / "util" / "job.sh"
    shutil.copytree(REPO_ROOT / "src" / "loopzero" / "kernel", script.parent)
    repo.chmod(0o700)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return script


@pytest.mark.parametrize("candidate_store_state", ("present", "parent-only", "absent"))
@requires_nested_user_namespace
def test_bound_sandbox_denies_continuation_candidate_store_writes(
    tmp_path: Path,
    candidate_store_state: str,
) -> None:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    continuation_root = repo / ".audit" / "delivery-continuations"
    candidate_store = continuation_root / "candidates"
    if candidate_store_state == "present":
        continuation_root.mkdir(parents=True)
        candidate_store.mkdir()
    elif candidate_store_state == "parent-only":
        continuation_root.mkdir(parents=True)
    forged = candidate_store / ("f" * 64 + ".json")
    moved = continuation_root.with_name("delivery-continuations-moved")
    writable_probe = repo / "sandbox-write-probe"
    artifact = tmp_path / "terminal.json"
    artifact.write_text(
        json.dumps({"task_id": "dispatch-closeout", "status": "completed"}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(script),
            "run",
            "bound-candidate-store",
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
            'if ! touch "$5"; then exit 7; fi; rm "$5"; '
            'if touch "$6"; then exit 10; fi; '
            'if mv "$3" "$4"; then exit 8; fi; '
            'if mkdir -p "$1" && touch "$2"; then exit 9; fi',
            "sh",
            str(candidate_store),
            str(forged),
            str(continuation_root),
            str(moved),
            str(writable_probe),
            str(repo / ".git" / "forged-config"),
        ],
        cwd=repo,
        env=consumer_environment(repo, tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not forged.exists()
    assert not moved.exists()
    assert not writable_probe.exists()


@pytest.mark.parametrize("variable", ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"))
def test_candidate_store_derivation_ignores_hostile_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    from loopzero.kernel import jobs as job_store

    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    monkeypatch.setattr(job_store, "__file__", str(script.with_name("job_store.py")))
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=attacker, check=True)
    monkeypatch.setenv(variable, str(attacker / ".git"))

    protected = job_store._canonical_candidate_protection_paths(repo)
    candidate_store = protected[-1]

    assert protected[0] == repo / ".git"
    assert candidate_store == (
        repo / ".audit" / "delivery-continuations" / "candidates"
    )
    assert candidate_store.is_dir()
    assert stat.S_IMODE(candidate_store.stat().st_mode) == 0o700


def test_candidate_store_protection_pins_linked_worktree_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loopzero.kernel import jobs as job_store

    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=primary, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        cwd=primary,
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(linked)],
        cwd=primary,
        check=True,
    )
    monkeypatch.setattr(
        job_store, "__file__", str(linked / "scripts" / "util" / "job_store.py")
    )

    protected = job_store._canonical_candidate_protection_paths(linked)
    git_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=linked,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    assert protected == (
        linked / ".git",
        git_dir,
        primary / ".git",
        primary / ".audit" / "delivery-continuations" / "candidates",
    )


@pytest.mark.parametrize(
    "entrypoint", ("direct", "host", "host-repo", "host-repo-equals")
)
def test_bound_host_dispatcher_accepts_its_canonical_task_result(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    host_dispatcher = script.with_name("agent_dispatch_host.py")
    if entrypoint == "direct":
        host_dispatcher = script.with_name("agent_dispatch.py")
    task_id = "dispatch-canonical-result"
    admin = repo / ".git" / "worktrees" / "delivery"
    admin.mkdir(parents=True)
    artifact = repo / ".audit" / "dispatch" / "results" / f"{task_id}.json"
    artifact.parent.mkdir(parents=True)
    host_dispatcher.write_text(
        "from pathlib import Path\n"
        "from loopzero.kernel.worktree_lease import worktree_lease\n"
        f"with worktree_lease(Path({str(repo)!r}), boundary='dispatch-test', git_directory=Path({str(admin)!r})):\n"
        f"    Path({str(artifact)!r}).write_text("
        f"{json.dumps(json.dumps({'task_id': task_id, 'status': 'completed'}))}, "
        "encoding='utf-8')\n",
        encoding="utf-8",
    )
    arguments = ["run"]
    if entrypoint == "host-repo":
        arguments = ["--repo", str(repo), "run"]
    elif entrypoint == "host-repo-equals":
        arguments = [f"--repo={repo}", "run"]

    result = subprocess.run(
        [
            str(script),
            "run",
            "bound-host-canonical-result",
            "--timeout",
            "30",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            task_id,
            "--terminal-artifact",
            str(artifact),
            "--",
            sys.executable,
            str(host_dispatcher),
            *arguments,
        ],
        cwd=repo,
        env=consumer_environment(repo, tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (admin / "worktree-boundary.lock").exists()
    assert not (admin / "worktree-boundary.owner.json").exists()
    assert json.loads(artifact.read_text(encoding="utf-8")) == {
        "task_id": task_id,
        "status": "completed",
    }


@pytest.mark.parametrize("linked_component", ("file", "directory"))
def test_bound_dispatcher_preserves_external_symlink_artifact(
    tmp_path: Path, linked_component: str
) -> None:
    script = _isolated_job_script(tmp_path)
    repo = script.resolve().parents[2]
    dispatcher = script.with_name("agent_dispatch_host.py")
    target_dir = tmp_path / "external"
    target_dir.mkdir()
    target = target_dir / "result.json"
    alias = tmp_path / "alias"
    if linked_component == "file":
        alias.symlink_to(target)
        artifact = alias
    else:
        alias.symlink_to(target_dir, target_is_directory=True)
        artifact = alias / "result.json"
    payload = json.dumps({"task_id": "dispatch-external", "status": "completed"})
    dispatcher.write_text(
        "from pathlib import Path\n"
        + f"Path({str(target)!r}).write_text({payload!r})\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(script),
            "run",
            "external-link",
            "--timeout",
            "30",
            "--run-id",
            "sr_" + "a" * 32,
            "--task-id",
            "dispatch-external",
            "--terminal-artifact",
            str(artifact),
            "--",
            sys.executable,
            str(dispatcher),
            "run",
        ],
        cwd=repo,
        env=consumer_environment(repo, tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(target.read_text()) == json.loads(payload)


def consumer_environment(repo, tmp_path):
    from loopzero.kernel.settings import KernelSettings
    settings = KernelSettings(env_prefix="INTELFLO", toolchain={
        "dispatcher": str(repo / "scripts/util/agent_dispatch.py"),
        "host_dispatcher": str(repo / "scripts/util/agent_dispatch_host.py"),
    })
    return package_environment({"PATH": "/usr/bin:/bin", "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
                                **settings.child_environment()})
