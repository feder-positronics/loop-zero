import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import git, git_environment

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/size_budget.sh"


def test_executable_scripts_keep_lf_in_autocrlf_checkout(git_repo, tmp_path):
    root = SCRIPT.parents[1]
    paths = ("scripts/size_budget.sh", "scripts/retro_metrics.py")
    for name in (*paths, ".gitattributes"):
        source = root / name
        if source.is_file():
            target = git_repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "add scripts")
    checkout = tmp_path / "Windows checkout"
    git(tmp_path, "clone", "-c", "core.autocrlf=true", str(git_repo), str(checkout))

    done = subprocess.run(["scripts/size_budget.sh"], cwd=checkout, env=git_environment(),
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    eol = git(checkout, "ls-files", "--eol", "--", *paths).splitlines()
    assert len(eol) == len(paths)
    assert all(line.split()[1] == "w/lf" for line in eol)


@pytest.mark.parametrize("failure", [None, "deleted", "show"])
def test_size_budget_counts_unicode_and_propagates_read_errors(git_repo, fake_tool, failure):
    source = git_repo / "src/loopzero" / ("a.py" if failure else os.fsdecode(b"\xc3\xa9 space\nname.py"))
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\n")
    source.with_name("z.py").write_text("three\n")
    base = stage_tree(git_repo)
    if failure == "deleted":  # unstaged deletion: skipped, not an error
        source.unlink()
    elif failure == "show":
        fake_tool("git", 'if [ "$1" = show ]; then case "$2" in */a.py) exit 1;; esac; fi\n'
                  f'exec {shutil.which("git")} "$@"\n')
    done = run_budget(git_repo, base)
    assert bool(done.returncode) is (failure == "show")
    if failure != "show":
        assert done.stdout.startswith("src=1 (base 3," if failure else "src=3 (base 3, delta 0,")


def run_budget(repo, base):
    return subprocess.run(["bash", str(SCRIPT), base], cwd=repo, env=git_environment(),
                          capture_output=True, text=True, check=False)


def stage_tree(repo):
    git(repo, "add", ".")
    return git(repo, "write-tree").strip()


@pytest.mark.parametrize("path,cap", [("core/CONTRACT.md", 1500), ("docs/DELEGATION.md", 660)])
@pytest.mark.parametrize("case", ["grow", "hold", "symlink"])
def test_markdown_doc_budgets(git_repo, path, cap, case):
    doc = git_repo / path
    doc.parent.mkdir(parents=True)
    target = git_repo.parent / "external.md" if case == "symlink" else doc
    target.write_text("line\n" * (cap + 5))
    if case == "symlink":
        doc.symlink_to(target)
    base = stage_tree(git_repo)
    if case == "grow":
        doc.write_text("line\n" * (cap + 6))
    done = run_budget(git_repo, base)
    assert done.returncode == int(case == "grow"), done.stdout + done.stderr
