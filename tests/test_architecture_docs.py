"""Exercise the documentation check as a contributor/CI subprocess in real Git."""
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import git

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_architecture_docs.py"


def prepare(repo):
    for name in ("docs/ARCHITECTURE.md", "docs/README.md", "README.md", "AGENTS.md",
                 "core/CONTRACT.md"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Current\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "architecture base")
    return git(repo, "rev-parse", "HEAD").strip()


def run(repo, base, *args):
    return subprocess.run([sys.executable, str(SCRIPT), base, *args], cwd=repo,
                          text=True, capture_output=True, check=False)


@pytest.mark.parametrize("sync", ["missing", "guide", "trailer", "empty", "squash"])
def test_architecture_change_requires_update_or_reasoned_review(git_repo, sync):
    base = prepare(git_repo)
    git(git_repo, "checkout", "-b", "authored")
    (git_repo / "core/CONTRACT.md").write_text("# New authority\n")
    if sync == "guide":
        (git_repo / "docs/ARCHITECTURE.md").write_text("# New authority explained\n")
    if sync in {"trailer", "empty", "squash"}:
        reason = "New wording preserves the documented authority boundary." if sync != "empty" else ""
        git(git_repo, "add", ".")
        git(git_repo, "commit", "-m", "Update\n\nArchitecture-Review: unchanged; " + reason)
    if sync == "squash":
        git(git_repo, "checkout", "main")
        git(git_repo, "merge", "--squash", "authored")
        git(git_repo, "commit", "-m", "Squash without the source trailer")
    done = run(git_repo, base)
    assert done.returncode == (0 if sync in {"guide", "trailer"} else 1)
    if done.returncode:
        assert "without guide update or reasoned Architecture-Review trailer" in done.stderr
    if sync == "squash":
        assert run(git_repo, base, "--links-only").returncode == 0
        (git_repo / "README.md").write_text("[broken](absent.md)\n")
        assert run(git_repo, base, "--links-only").returncode == 1


@pytest.mark.parametrize("target", ["absent.md", "../README.md#absent"])
def test_broken_local_link_fails_even_with_guide_update(git_repo, target):
    base = prepare(git_repo)
    (git_repo / "docs/ARCHITECTURE.md").write_text(f"# Current\n[reference]({target})\n")
    done = run(git_repo, base)
    assert done.returncode == 1
    assert "invalid local" in done.stderr


def test_private_code_fix_needs_no_pointless_documentation_edit(git_repo):
    base = prepare(git_repo)
    path = git_repo / "src/loopzero/cli.py"
    path.parent.mkdir(parents=True)
    path.write_text("def build_parser():\n    return 'public'\n\ndef _fix():\n    return 1\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "public surface\n\nArchitecture-Review: unchanged; "
        "Existing guide already describes this command interface.")
    base = git(git_repo, "rev-parse", "HEAD").strip()
    path.write_text(path.read_text().replace("return 1", "return 2"))
    assert run(git_repo, base).returncode == 0
    path.write_text(path.read_text().replace("'public'", "'changed'"))
    assert run(git_repo, base).returncode == 1


def test_missing_base_fails_closed(git_repo):
    prepare(git_repo)
    assert run(git_repo, "not-a-revision").returncode == 1
