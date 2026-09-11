import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from conftest import REPO
from loopzero import __version__, cli


def run(*argv: str) -> int:
    return cli.main(list(argv))


def test_version_matches_core_version_file():
    assert __version__ == (REPO / "core/VERSION").read_text().strip()


def test_init_writes_starter_and_refuses_overwrite(tmp_path: Path, capsys):
    assert run("--root", str(tmp_path), "init", "--revision", "a" * 40) == 0
    assert (tmp_path / "workflow.toml").exists()
    assert run("--root", str(tmp_path), "init") == 1
    assert "exists" in capsys.readouterr().err


def test_sync_check_fails_before_sync_and_passes_after(consumer: Path, capsys):
    assert run("--root", str(consumer), "sync", "--check") == 1
    assert "drift" in capsys.readouterr().err
    assert run("--root", str(consumer), "sync") == 0
    assert run("--root", str(consumer), "sync", "--check") == 0


def test_status_compares_package_and_snapshot(consumer: Path, capsys):
    assert run("--root", str(consumer), "status") == 0
    output = capsys.readouterr().out
    assert "version equality: equal" in output
    assert "pass" not in output
    (consumer / "vendor/loop-zero/VERSION").write_text("0.0.0\n")
    assert run("--root", str(consumer), "status") == 0
    output = capsys.readouterr().out
    assert "version equality: different" in output
    assert "pass" not in output


def test_status_uses_only_trusted_source_verifier(consumer: Path, capsys, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(REPO / "core", source / "core")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "core"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "trusted source",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    workflow = consumer / "workflow.toml"
    workflow.write_text(workflow.read_text().replace("0123456789abcdef0123456789abcdef01234567", revision))
    shutil.rmtree(consumer / "vendor/loop-zero")
    shutil.copytree(source / "core", consumer / "vendor/loop-zero")

    assert run("--root", str(consumer), "status", "--source", str(source)) == 0
    assert capsys.readouterr().out.strip().endswith("pass")

    marker = tmp_path / "candidate-verifier-ran"
    (consumer / "vendor/loop-zero/tools/status.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    )
    assert run("--root", str(consumer), "status", "--source", str(source)) == 1
    assert not marker.exists()
    assert "UNVERIFIED" in capsys.readouterr().err


def test_policy_lint_reports_problems_and_base_hooks(consumer: Path, capsys):
    assert run("--root", str(consumer), "policy", "lint") == 1
    assert "UNVERIFIED" in capsys.readouterr().err
    assert run("--root", str(consumer), "policy", "lint", "--no-hooks") == 0
    assert "pass" not in capsys.readouterr().out
    base = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 0
    assert run("--root", str(consumer), "policy", "lint", "--base-ref", "main") == 0
    assert base in capsys.readouterr().out
    assert run("--root", str(consumer), "policy", "lint", "--base", "main") == 1
    (consumer / "workflow.toml").write_text("[core]\n", encoding="utf-8")
    assert run("--root", str(consumer), "policy", "lint", "--no-hooks") == 1
    assert "[core].revision" in capsys.readouterr().err


def test_checks_matches_snapshot_tool_without_executing_it(consumer: Path, capfd, tmp_path: Path):
    results = consumer / "results.json"
    results.write_text(json.dumps({"tests": {"conclusion": "network_timeout", "attempts": 2}}))
    snapshot_tool = consumer / "vendor/loop-zero/tools/checks.py"
    expected = subprocess.run(
        [
            sys.executable,
            str(snapshot_tool),
            "checks",
            "--workflow",
            str(consumer / "workflow.toml"),
            "--results",
            str(results),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    marker = tmp_path / "snapshot-ran"
    snapshot_tool.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
    rc = run("--root", str(consumer), "checks", "--results", str(results))
    assert rc == 0
    output = capfd.readouterr().out
    assert output == expected
    assert not marker.exists()
    report = json.loads(output)
    entry = next(item for item in report if item["name"] == "tests")
    assert entry["blocks"] is True


def test_hook_commands_reject_shell_syntax_and_inherited_path(consumer: Path, capsys, tmp_path: Path, monkeypatch):
    base = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    workflow = consumer / "workflow.toml"
    original = workflow.read_text()
    for command in ("true && false", "true || false", "true; false", "true | false", "`false`", "echo $(false)"):
        workflow.write_text(original.replace('worktree_setup = ["make setup"]', f'worktree_setup = ["{command}"]'))
        assert run("--root", str(consumer), "policy", "lint", "--base", base) == 1
        assert "metacharacters" in capsys.readouterr().err

    shadow = tmp_path / "shadow"
    shadow.mkdir()
    executable = shadow / "shadow-only"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(shadow))
    make_shadow = shadow / "make"
    make_shadow.write_text("#!/bin/sh\nexit 99\n")
    make_shadow.chmod(make_shadow.stat().st_mode | stat.S_IXUSR)
    workflow.write_text(original)
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 0
    capsys.readouterr()

    workflow.write_text(original.replace('worktree_setup = ["make setup"]', 'worktree_setup = ["shadow-only"]'))
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 1
    assert "allowed PATH" in capsys.readouterr().err

    repository_hook = consumer / "hook-script"
    repository_hook.write_text("#!/bin/sh\nexit 0\n")
    repository_hook.chmod(repository_hook.stat().st_mode | stat.S_IXUSR)
    workflow.write_text(original.replace('worktree_setup = ["make setup"]', 'worktree_setup = ["./hook-script"]'))
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 0
    capsys.readouterr()

    outside_hook = tmp_path / "outside-hook"
    outside_hook.write_text("#!/bin/sh\nexit 0\n")
    outside_hook.chmod(outside_hook.stat().st_mode | stat.S_IXUSR)
    workflow.write_text(original.replace('worktree_setup = ["make setup"]', 'worktree_setup = ["../outside-hook"]'))
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 1
    assert "allowed PATH" in capsys.readouterr().err


def test_non_commit_base_and_symlinked_base_workflow_fail(consumer: Path, capsys):
    blob = subprocess.run(
        ["git", "-C", str(consumer), "hash-object", "workflow.toml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert run("--root", str(consumer), "policy", "lint", "--base", blob) == 1
    assert "unavailable" in capsys.readouterr().err

    workflow = consumer / "workflow.toml"
    contents = workflow.read_text()
    (consumer / "linked-workflow.toml").write_text(contents)
    workflow.unlink()
    workflow.symlink_to("linked-workflow.toml")
    subprocess.run(["git", "-C", str(consumer), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(consumer),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "symlink policy",
        ],
        check=True,
    )
    symlink_base = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    workflow.unlink()
    workflow.write_text(contents)
    assert run("--root", str(consumer), "policy", "lint", "--base", symlink_base) == 1
    assert "regular blob" in capsys.readouterr().err


def test_base_hooks_use_candidate_schema_validation(consumer: Path, capsys):
    workflow = consumer / "workflow.toml"
    contents = workflow.read_text()
    bad_base = "hooks = []\n" + contents.replace(
        '[hooks]\nworktree_setup = ["make setup"]\nacceptance = ["make test"]\n', ""
    )
    workflow.write_text(bad_base)
    subprocess.run(["git", "-C", str(consumer), "add", "workflow.toml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(consumer),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "invalid base hooks",
        ],
        check=True,
    )
    base = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    workflow.write_text(contents)
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 1
    assert "must be a table" in capsys.readouterr().err


def test_base_policy_ignores_git_replacement_objects(consumer: Path, capsys):
    workflow = consumer / "workflow.toml"
    contents = workflow.read_text()
    base = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    workflow.write_text(
        "hooks = []\n"
        + contents.replace(
            '[hooks]\nworktree_setup = ["make setup"]\nacceptance = ["make test"]\n', ""
        )
    )
    subprocess.run(["git", "-C", str(consumer), "add", "workflow.toml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(consumer),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "replacement policy",
        ],
        check=True,
    )
    replacement = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(consumer), "replace", base, replacement], check=True)
    workflow.write_text(contents)
    assert run("--root", str(consumer), "policy", "lint", "--base", base) == 0
    assert capsys.readouterr().out.strip() == "pass"


def test_config_errors_print_unverified(tmp_path: Path, capsys):
    assert run("--root", str(tmp_path), "sync", "--check") == 1
    assert "UNVERIFIED" in capsys.readouterr().err
