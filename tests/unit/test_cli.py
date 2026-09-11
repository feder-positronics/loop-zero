import json
from pathlib import Path

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
    assert capsys.readouterr().out.strip().endswith("pass")
    (consumer / "vendor/loop-zero/VERSION").write_text("0.0.0\n")
    assert run("--root", str(consumer), "status") == 1
    assert "mismatch" in capsys.readouterr().err


def test_policy_lint_reports_problems_and_base_hooks(consumer: Path, capsys):
    assert run("--root", str(consumer), "policy", "lint") == 0
    assert run("--root", str(consumer), "policy", "lint", "--base", "main") == 0
    (consumer / "workflow.toml").write_text("[core]\n", encoding="utf-8")
    assert run("--root", str(consumer), "policy", "lint") == 1
    assert "[core].revision" in capsys.readouterr().err


def test_checks_delegates_to_snapshot_tool(consumer: Path, capfd):
    results = consumer / "results.json"
    results.write_text(json.dumps({"tests": {"conclusion": "network_timeout", "attempts": 2}}))
    rc = run("--root", str(consumer), "checks", "--results", str(results))
    assert rc == 0
    report = json.loads(capfd.readouterr().out)
    entry = next(item for item in report if item["name"] == "tests")
    assert entry["blocks"] is True


def test_config_errors_print_unverified(tmp_path: Path, capsys):
    assert run("--root", str(tmp_path), "sync", "--check") == 1
    assert "UNVERIFIED" in capsys.readouterr().err
