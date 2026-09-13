"""Exercise the example consumer in process from the source checkout.

CI separately copies this fixture and exercises the installed artifact's
public commands inside its validation sandbox.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from conftest import REPO, git
from loopzero import cli

FIXTURE = REPO / "tests" / "example_consumer"


def deploy(tmp_path: Path) -> Path:
    root = tmp_path / "example"
    shutil.copytree(FIXTURE, root)
    shutil.copytree(REPO / "core", root / "vendor" / "loop-zero")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    return root


def test_example_consumer_end_to_end(tmp_path: Path, capsys):
    root = deploy(tmp_path)
    assert cli.main(
        ["--root", str(root), "policy", "lint", "--base-ref", "refs/heads/main"]
    ) == 0
    assert cli.main(["--root", str(root), "sync", "--check"]) == 1
    product_skill = root / ".cursor/skills/example-product/SKILL.md"
    product_before = product_skill.read_bytes()
    assert cli.main(["--root", str(root), "sync"]) == 0
    assert cli.main(["--root", str(root), "sync", "--check"]) == 0
    assert cli.main(["--root", str(root), "status"]) == 0
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# Example consumer") and "adapters/codex.md" in text
    assert product_skill.read_bytes() == product_before
    assert (root / ".agents/skills/example-product").is_symlink()
    assert (root / ".cursor/skills/tdd/SKILL.md").is_file()
    capsys.readouterr()


def test_snapshot_tools_still_run_from_the_consumer(tmp_path: Path):
    root = deploy(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(root / "vendor/loop-zero/tools/checks.py"), "checks", "--workflow", str(root / "workflow.toml")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"unit"' in proc.stdout
