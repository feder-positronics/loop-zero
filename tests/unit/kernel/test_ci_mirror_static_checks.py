
from .package_environment import package_environment
import subprocess
import sys
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_hook_repo(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[3]
    repo = tmp_path / "repo"
    repo.mkdir()
    hook = root / "src" / "loopzero" / "hooks" / "ci-mirror-check.sh"
    _write_executable(
        repo / "scripts" / "hooks" / "ci-mirror-check.sh",
        hook.read_text(encoding="utf-8"),
    )
    _write_executable(
        repo / "scripts" / "util" / "job.sh", "#!/usr/bin/env bash\nexit 0\n"
    )
    kernel = repo / "scripts/kernel"
    kernel.mkdir()
    (kernel / "settings.sh").write_text((root / "src/loopzero/kernel/settings.sh").read_text())
    (kernel / "job.sh").write_text("#!/bin/sh\nexit 0\n")
    (kernel / "job.sh").chmod(0o755)
    frontend_file = repo / "nextjs-frontend" / "fixture.ts"
    frontend_file.parent.mkdir(parents=True)
    frontend_file.write_text("export const fixture = 1;\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "static-check@example.test")
    _git(repo, "config", "user.name", "Static Check Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    frontend_file.write_text("export const fixture = 2;\n", encoding="utf-8")
    return repo


def _install_command_stubs(bin_dir: Path) -> None:
    _write_executable(
        bin_dir / "make",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        bin_dir / "gh",
        "#!/usr/bin/env bash\nexit 1\n",
    )
    _write_executable(
        bin_dir / "pnpm",
        """#!/usr/bin/env bash
printf 'pnpm %s\\n' "$*" >> "$CI_MIRROR_TEST_LOG"
if [ "$1" = "run" ] && [ "$2" = "tsc" ] && [ "${CI_MIRROR_FAIL_STEP:-}" = "tsc" ]; then
    exit 7
fi
if [ "$1" = "run" ] && [ "$2" = "lint" ] && [ "${CI_MIRROR_FAIL_STEP:-}" = "lint" ]; then
    exit 8
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "python3",
        f"""#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
    exec {sys.executable} "$@"
fi
if [ "$1" = "-m" ] && [ "$2" = "loopzero.kernel.events" ]; then
    shift 2
    set -- scripts/util/agent_event.py "$@"
fi
case "$1" in
scripts/util/ci_mirror_receipts.py)
    if [[ " $* " == *" --capture-digest "* ]]; then
        printf 'fixture-digest\\n'
    else
        printf 'receipt %s\\n' "$*" >> "$CI_MIRROR_TEST_LOG"
    fi
    ;;
scripts/util/agent_event.py)
    if [ "$2" = "runtime-kind" ]; then
        printf 'human\\n'
    else
        printf 'event %s\\n' "$*" >> "$CI_MIRROR_TEST_LOG"
    fi
    ;;
scripts/util/frontend_validation_scope.py)
    printf 'full\\n'
    ;;
scripts/util/skill_semantic_paths.py | scripts/util/poll_audit.py)
    ;;
*)
    printf 'unexpected python invocation: %s\\n' "$*" >&2
    exit 1
    ;;
esac
""",
    )


def _run_hook(
    tmp_path: Path, failure: str | None, *, packaged: bool = False,
    audit_root: str = ".audit",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    repo = _prepare_hook_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_command_stubs(bin_dir)
    log = tmp_path / "commands.log"
    env = {
        "CI_MIRROR_BASE_REF": "main",
        "LOOPZERO_BACKEND_ROOT": "fastapi_backend",
        "LOOPZERO_FRONTEND_ROOT": "nextjs-frontend",
        "CI_MIRROR_FAIL_STEP": failure or "",
        "CI_MIRROR_TEST_LOG": str(log),
        "LOOPZERO_AUDIT_ROOT": audit_root,
        "INTELFLO_JOB_DIR": str(tmp_path / "jobs"),
        "LANG": "C.UTF-8",
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
    }
    for name in ("ci_mirror_receipts", "frontend_validation_scope", "poll_audit", "skill_convergence", "skill_semantic_paths"):
        hook = bin_dir / (name + "-hook")
        if name == "poll_audit":
            hook.write_text(
                f'#!/bin/sh\nprintf "poll %s\\n" "$*" >> "{log}"\n'
            )
        else:
            hook.write_text('#!/bin/sh\nexec "' + str(bin_dir / "python3") + '" scripts/util/' + name + '.py "$@"\n')
        hook.chmod(0o755)
        env["INTELFLO_" + name.upper() + "_HOOK"] = str(hook)
    lane_commands = {
        "ACTIONS_BRANCH_POLICY": "make check-actions-branch-policy",
        "REPO_WORKFLOW_POLICY": "make check-repo-workflow-policy",
        "BLUEPRINT_DRIFT": "make blueprint-drift-check",
        "COMPLEXITY": "make guardian-complexity-check",
        "BACKEND_LINT": "true", "BACKEND_TYPES": "true",
        "FRONTEND_TYPES": "pnpm run tsc", "FRONTEND_LINT": "pnpm run lint",
        "FRONTEND_FORMAT": "pnpm exec prettier --check", "FRONTEND_TESTS": "pnpm exec vitest",
        "AGENT_CONFIGS": "make check-agent-configs",
        "SKILL_CONVERGENCE_TESTS": "make check-skill-convergence-tests",
    }
    for name, command in lane_commands.items():
        lane = bin_dir / name.lower()
        lane.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
        lane.chmod(0o755)
        env["INTELFLO_" + name + "_HOOK"] = str(lane)
    env = package_environment(env)
    if not packaged:
        env["LOOPZERO_PYTHON"] = str(bin_dir / "python3")
    hook_path = (
        Path(__file__).resolve().parents[3] / "src/loopzero/hooks/ci-mirror-check.sh"
        if packaged
        else Path("scripts/hooks/ci-mirror-check.sh")
    )
    cwd = repo / "nextjs-frontend" if packaged else repo
    result = subprocess.run(
        ["bash", str(hook_path)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("failure", ("tsc", "lint"))
def test_frontend_static_failure_short_circuits_and_records_failure(
    tmp_path: Path, failure: str
) -> None:
    result, commands = _run_hook(tmp_path, failure)

    assert result.returncode != 0
    assert "pnpm run tsc" in commands
    assert (
        "receipt scripts/util/ci_mirror_receipts.py --category frontend-static --base-ref main --result fail --expected-digest fixture-digest"
        in commands
    )
    frontend_static_events = [
        command for command in commands if "frontend-static" in command
    ]
    assert (
        "event scripts/util/agent_event.py tool --skill ci-mirror-check --category frontend-static"
        in "\n".join(frontend_static_events)
    )
    assert "--result fail" in "\n".join(frontend_static_events)
    assert not any("--result pass" in command for command in frontend_static_events)
    if failure == "tsc":
        assert "pnpm run lint" not in commands
        assert not any("pnpm exec prettier" in command for command in commands)
    else:
        assert "pnpm run lint" in commands
        assert not any("pnpm exec prettier" in command for command in commands)


def test_frontend_static_success_runs_every_check_and_records_pass(
    tmp_path: Path,
) -> None:
    result, commands = _run_hook(tmp_path, None)

    assert result.returncode == 0, result.stderr
    assert "pnpm run tsc" in commands
    assert "pnpm run lint" in commands
    assert any("pnpm exec prettier --check" in command for command in commands)
    assert (
        "receipt scripts/util/ci_mirror_receipts.py --category frontend-static --base-ref main --result pass --expected-digest fixture-digest"
        in commands
    )
    frontend_static_events = [
        command for command in commands if "frontend-static" in command
    ]
    assert (
        "event scripts/util/agent_event.py tool --skill ci-mirror-check --category frontend-static"
        in "\n".join(frontend_static_events)
    )
    assert "--result pass" in "\n".join(frontend_static_events)


def test_packaged_hook_discovers_consumer_and_configured_audit_root(tmp_path: Path) -> None:
    result, commands = _run_hook(
        tmp_path, None, packaged=True, audit_root="evidence"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    poll = next(command for command in commands if command.startswith("poll "))
    assert "--skill-runs-dir" in poll
    assert "/evidence/skill-runs" in poll
