import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import git

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/size_budget.sh"


def _branchy(name: str, branches: int) -> str:
    body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(branches))
    return f"def {name}(x):\n{body}    return -1\n"


@pytest.mark.parametrize("failure", [None, "deleted", "show"])
def test_size_budget_counts_unicode_and_propagates_read_errors(git_repo, fake_tool, failure):
    source = git_repo / "src/loopzero" / ("a.py" if failure else "é space\nname.py")
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\n")
    source.with_name("z.py").write_text("three\n")
    git(git_repo, "add", ".")
    base = git(git_repo, "write-tree").strip()
    if failure == "deleted":  # unstaged deletion: skipped, not an error
        source.unlink()
    elif failure == "show":
        fake_tool("git", 'if [ "$1" = show ]; then case "$2" in */a.py) exit 1;; esac; fi\n'
                  f'exec {shutil.which("git")} "$@"\n')
    done = subprocess.run(["bash", str(SCRIPT), base], cwd=git_repo,
                          capture_output=True, text=True, check=False)
    assert bool(done.returncode) is (failure == "show")
    if failure != "show":
        assert done.stdout.startswith("src=1 (base 3," if failure else "src=3 (base 3, delta 0,")


@pytest.mark.parametrize("case,expected_rc", [
    ("new_over_cap", 1),
    ("existing_hold", 0),
    ("existing_shrink", 0),
    ("existing_grow", 1),
])
def test_complexity_ratchet(git_repo, case, expected_rc):
    src = git_repo / "src/loopzero/mod.py"
    src.parent.mkdir(parents=True)
    if case == "new_over_cap":
        src.write_text("def ok():\n    return 1\n")
    else:
        src.write_text(_branchy("hot", 11))  # CC = 12 > 10
    git(git_repo, "add", ".")
    base = git(git_repo, "write-tree").strip()

    if case == "new_over_cap":
        src.write_text(_branchy("new_hot", 10))  # CC = 11 > 10
    elif case == "existing_shrink":
        src.write_text(_branchy("hot", 10))  # CC = 11 <= 12
    elif case == "existing_grow":
        src.write_text(_branchy("hot", 12))  # CC = 13 > 12

    done = subprocess.run(["bash", str(SCRIPT), base], cwd=git_repo,
                          capture_output=True, text=True, check=False)
    assert done.returncode == expected_rc, done.stdout + done.stderr
    assert ("complexity=FAIL" if expected_rc else "complexity=PASS") in done.stdout


def test_complexity_parses_pep758_and_ignores_with_blocks(git_repo):
    src = git_repo / "src/loopzero/mod.py"
    src.parent.mkdir(parents=True)
    with_blocks = "".join(f"    with contextlib.suppress(ValueError):\n        y = {i}\n"
                          for i in range(11))
    src.write_text(
        "import contextlib\n"
        "def pep758(x):\n"
        f"{with_blocks}"
        "    try:\n"
        "        return int(x) + y\n"
        "    except KeyError, TypeError:\n"
        "        return 0\n"
    )
    git(git_repo, "add", ".")
    base = git(git_repo, "write-tree").strip()
    done = subprocess.run(["bash", str(SCRIPT), base], cwd=git_repo,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "complexity=PASS (cap 10, over_cap 0, regressions 0)" in done.stdout


def test_complexity_rejects_old_python(git_repo, tmp_path):
    src = git_repo / "src/loopzero/mod.py"
    src.parent.mkdir(parents=True)
    src.write_text("def ok():\n    return 1\n")
    git(git_repo, "add", ".")
    base = git(git_repo, "write-tree").strip()
    fake_py = tmp_path / "old-python"
    fake_py.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.version_info = (3, 12, 0, 'final', 0)\n"
        "sys.argv = sys.argv[1:]\n"
        "exec(compile(sys.stdin.read(), '<size_budget>', 'exec'), {'__name__': '__main__'})\n"
    )
    fake_py.chmod(0o755)
    env = {**os.environ, "PYTHON": str(fake_py)}
    done = subprocess.run(["bash", str(SCRIPT), base], cwd=git_repo, env=env,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 1
    assert "requires Python 3.14+" in done.stderr


@pytest.mark.parametrize("path,cap,grew,expected_rc", [
    ("core/CONTRACT.md", 1500, True, 1),
    ("core/CONTRACT.md", 1500, False, 0),
    ("docs/DELEGATION.md", 660, True, 1),
    ("docs/DELEGATION.md", 660, False, 0),
])
def test_markdown_doc_budgets(git_repo, path, cap, grew, expected_rc):
    doc = git_repo / path
    doc.parent.mkdir(parents=True)
    doc.write_text("line\n" * (cap + 5))
    git(git_repo, "add", ".")
    base = git(git_repo, "write-tree").strip()
    if grew:
        doc.write_text("line\n" * (cap + 6))
    done = subprocess.run(["bash", str(SCRIPT), base], cwd=git_repo,
                          capture_output=True, text=True, check=False)
    assert done.returncode == expected_rc, done.stdout + done.stderr
