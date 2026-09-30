import subprocess
from pathlib import Path

import pytest

from tests.conftest import git


@pytest.mark.parametrize("failure", [None, "cat", "show"])
def test_size_budget_counts_unicode_and_propagates_read_errors(git_repo, fake_tool, failure):
    script = Path(__file__).resolve().parents[1] / "scripts/size_budget.sh"
    source = git_repo / "src/loopzero" / ("a.py" if failure else "é space\nname.py")
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\n")
    source.with_name("z.py").write_text("three\n")
    git(git_repo, "add", ".")
    base = git(git_repo, "write-tree").strip()
    if failure == "cat":
        source.unlink()
    elif failure == "show":
        fake_tool("git", 'if [ "$1" = show ]; then case "$2" in */a.py) exit 1;; esac; fi\n'
                  'exec /usr/bin/git "$@"\n')
    done = subprocess.run(["bash", str(script), base], cwd=git_repo,
                          capture_output=True, text=True, check=False)
    assert bool(done.returncode) is bool(failure)
    if failure is None:
        assert done.stdout.splitlines()[0] == "src=3 (base 3, delta 0, cap 4550, headroom 4547)"
