"""Exercise the documentation check as a contributor/CI subprocess in real Git."""
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import git, git_environment

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_architecture_docs.py"


@pytest.fixture
def base(git_repo):
    for name in ("docs/ARCHITECTURE.md", "docs/README.md", "README.md", "AGENTS.md",
                 "core/CONTRACT.md", "workflow.toml"):
        path = git_repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Current\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "architecture base")
    return git(git_repo, "rev-parse", "HEAD").strip()


def run(repo, base, *args):
    return subprocess.run([sys.executable, str(SCRIPT), base, *args], cwd=repo, env=git_environment(),
                          text=True, capture_output=True, check=False)


@pytest.mark.parametrize("sync", ["missing", "guide", "trailer", "empty", "short", "squash",
                                  "untracked-non-utf8", "staged-non-utf8", "rename"])
def test_architecture_change_requires_update_or_reasoned_review(git_repo, base, sync):
    if sync.endswith("non-utf8"):
        if sys.platform != "linux":
            pytest.skip("requires raw pathname bytes")
        path = os.fsdecode(b"caf\xe9.txt")
        (git_repo / path).write_text("unrelated file\n")
        if sync == "staged-non-utf8":
            git(git_repo, "add", "--", path)
        done = run(git_repo, base)
        assert done.returncode == 0, done.stderr
    git(git_repo, "checkout", "-b", "authored")
    if sync == "rename":
        git(git_repo, "mv", "workflow.toml", "retired.toml")
        git(git_repo, "commit", "-m", "Move watched contract")
    else:
        (git_repo / "core/CONTRACT.md").write_text("# New authority\n")
    if sync == "guide":
        (git_repo / "docs/ARCHITECTURE.md").write_text("# New authority explained\n")
    if sync in {"trailer", "empty", "short", "squash"}:
        reason = {"empty": "", "short": "Same boundary"}.get(sync, "New wording preserves the documented authority boundary.")
        git(git_repo, "add", ".")
        git(git_repo, "commit", "-m", "Update\n\nArchitecture-Review: unchanged; " + reason)
    if sync == "squash":
        git(git_repo, "checkout", "main")
        git(git_repo, "merge", "--squash", "authored")
        git(git_repo, "commit", "-m", "Squash without the source trailer")
    done = run(git_repo, base)
    assert done.returncode == (0 if sync in {"guide", "trailer"} else 1)
    if sync in {"short", "empty"}:
        assert "Architecture-Review reason must be at least 20 characters" in done.stderr
    elif done.returncode:
        assert "without guide update or reasoned Architecture-Review trailer" in done.stderr
    if sync == "squash":
        git_repo = git_repo.rename(git_repo.with_name(f"{git_repo.name} "))
        assert run(git_repo, base, "--links-only").returncode == 0
        (git_repo / "README.md").write_text("[Guide][guide]\n\n[guide]: absent.md\n")
        assert run(git_repo, base, "--links-only").returncode == 1


@pytest.mark.parametrize("markdown", ["[ref](absent.md)", "[ref](../README.md#absent)", "[ref](<Design (legacy).md>)",
                                     "[ref](#explicit)", "[ref](../README.md 'title')",
                                     "~~~md\n[ref](missing.md)\n~~~", "[ref](#absent-heading)",
                                     "`[ref](missing.md)`", "`` `[ref](missing.md)` ``", "[`ref`](code`name`.md)",
                                     "API\n===\n\n[ref](#api)", "API\n---\n\n[ref](#api)",
                                     "# Alpha\n# Alpha-1\n# Alpha\n\n[ref](#alpha-2)"])
def test_local_links_even_with_guide_update(git_repo, base, markdown):
    (git_repo / "docs/Design (legacy).md").touch()
    (git_repo / "docs/code`name`.md").touch()
    (git_repo / "docs/ARCHITECTURE.md").write_text(f'<a id="explicit"></a>\n```md\n# Absent heading\n```\n{markdown}\n')
    done = run(git_repo, base)
    assert (done.returncode, "invalid local" in done.stderr) == (int("absent" in markdown), "absent" in markdown)


def test_interface_comparison_requires_valid_base_and_only_watches_public_changes(git_repo, base):
    path = git_repo / "src/loopzero/_cli_args.py"
    path.parent.mkdir(parents=True)
    path.write_text("def build_parser():\n    return 'public'\n\ndef _fix():\n    return 1\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "public surface\n\nArchitecture-Review: unchanged; "
        "Existing guide already describes this command interface.")
    base = git(git_repo, "rev-parse", "HEAD").strip()
    path.write_text(path.read_text().replace("return 1", "return 2"))
    assert run(git_repo, base).returncode == 0
    assert run(git_repo, "not-a-revision").returncode == 1
    path.write_text(path.read_text().replace("'public'", "'changed'"))
    assert run(git_repo, base).returncode == 1


def test_direct_git_timeout(monkeypatch, capsys):
    def stalled_git(argv, *, timeout, **kwargs):
        assert timeout == 60
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(subprocess, "run", stalled_git)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--links-only"])
    assert runpy.run_path(str(SCRIPT))["main"]() == 1
    assert "timed out" in capsys.readouterr().err


def test_tach_enforces_module_boundaries(tmp_path):
    import shutil

    repo_root = Path(__file__).resolve().parents[1]
    tach = Path(sys.executable).with_name("tach")
    assert tach.exists(), f"tach not installed next to {sys.executable}"
    shutil.copy2(repo_root / "tach.toml", tmp_path / "tach.toml")
    shutil.copytree(repo_root / "src", tmp_path / "src")
    types_py = tmp_path / "src/loopzero/types.py"
    types_py.write_text("from loopzero import cli\n" + types_py.read_text())
    broken = subprocess.run([str(tach), "check"], cwd=tmp_path,
                            text=True, capture_output=True, check=False)
    assert broken.returncode != 0, "tach check must reject reverse imports into types.py"
