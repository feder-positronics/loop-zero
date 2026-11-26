import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import git

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/size_budget.sh"


def test_executable_scripts_keep_lf_in_autocrlf_checkout(git_repo, tmp_path):
    root = SCRIPT.parents[1]
    paths = ("scripts/size_budget.sh", "scripts/retro_metrics.py", "scripts/ai-accounts/ai-accounts")
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

    done = subprocess.run(["scripts/size_budget.sh"], cwd=checkout,
                          env={**os.environ, "PYTHON": sys.executable},
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    eol = git(checkout, "ls-files", "--eol", "--", *paths).splitlines()
    assert len(eol) == len(paths)
    assert all(line.split()[1] == "w/lf" for line in eol)


def _branchy(name: str, branches: int) -> str:
    body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(branches))
    return f"def {name}(x):\n{body}    return -1\n"


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


def run_budget(repo, base, *, python=sys.executable):
    return subprocess.run(["bash", str(SCRIPT), base], cwd=repo,
                          env={**os.environ, "PYTHON": python},
                          capture_output=True, text=True, check=False)


def stage_tree(repo):
    git(repo, "add", ".")
    return git(repo, "write-tree").strip()


@pytest.fixture
def module_path(git_repo):
    path = git_repo / "src/loopzero/mod.py"
    path.parent.mkdir(parents=True)
    return path


_SIMPLE_FUNCTION = "def ok():\n    return 1\n"
_HOT_FUNCTION = _branchy("hot", 11)


_CONTEXT_MANAGERS = "".join(f"    with contextlib.suppress(ValueError):\n        y = {i}\n"
                            for i in range(11))
_PEP758 = "import contextlib\ndef pep758(x):\n" + _CONTEXT_MANAGERS + (
    "    try:\n        return int(x) + y\n    except KeyError, TypeError:\n        return 0\n"
)


@pytest.mark.parametrize("before,after,over_cap,regressions", [
    pytest.param(_SIMPLE_FUNCTION, _branchy("new_hot", 10), 1, 1, id="new_over_cap"),
    pytest.param(_HOT_FUNCTION, _HOT_FUNCTION, 1, 0, id="existing_hold"),
    pytest.param(_HOT_FUNCTION, _branchy("hot", 10), 1, 0, id="existing_shrink"),
    pytest.param(_HOT_FUNCTION, _branchy("hot", 12), 1, 1, id="existing_grow"),
    pytest.param(_PEP758, _PEP758, 0, 0, id="pep758_with_blocks"),
])
def test_complexity_ratchet(git_repo, module_path, before, after, over_cap, regressions):
    module_path.write_text(before)
    base = stage_tree(git_repo)
    if after != before:
        module_path.write_text(after)
    done = run_budget(git_repo, base)
    assert done.returncode == int(bool(regressions)), done.stdout + done.stderr
    result = "FAIL" if regressions else "PASS"
    assert f"complexity={result} (cap 10, over_cap {over_cap}, regressions {regressions})" in done.stdout


def test_complexity_rejects_old_python(git_repo, module_path, tmp_path):
    module_path.write_text(_SIMPLE_FUNCTION)
    base = stage_tree(git_repo)
    fake_py = tmp_path / "old-python"
    fake_py.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.version_info = (3, 12, 0, 'final', 0)\n"
        "sys.argv = sys.argv[1:]\n"
        "exec(compile(sys.stdin.read(), '<size_budget>', 'exec'), {'__name__': '__main__'})\n"
    )
    fake_py.chmod(0o755)
    done = run_budget(git_repo, base, python=str(fake_py))
    assert done.returncode == 1
    assert "requires Python 3.14+" in done.stderr


@pytest.mark.parametrize("path,cap", [("core/CONTRACT.md", 1500), ("docs/DELEGATION.md", 660)])
@pytest.mark.parametrize("grew", [True, False])
def test_markdown_doc_budgets(git_repo, path, cap, grew):
    doc = git_repo / path
    doc.parent.mkdir(parents=True)
    doc.write_text("line\n" * (cap + 5))
    base = stage_tree(git_repo)
    if grew:
        doc.write_text("line\n" * (cap + 6))
    done = run_budget(git_repo, base)
    assert done.returncode == int(grew), done.stdout + done.stderr
