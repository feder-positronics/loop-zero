"""Exercise publication with the actual rendered IntelFlo consumer facade."""

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.delivery import _publish_body as body_check

ROOT = Path(__file__).resolve().parents[3]
VALID_BODY = (
    "## Context and goal\n\n"
    "- **Context:** Teammates review changes.\n"
    "- **Problem:** The purpose can be unclear.\n"
    "- **Goal:** Explain the intended outcome.\n\n"
    "Closes #1\n\n## Validation\n\n- Owning checks passed.\n"
)


def test_trusted_base_executes_rendered_intelflo_facade(tmp_path, monkeypatch):
    # -I must load the package from the selected interpreter, not PYTHONPATH.
    venv = tmp_path / "interpreter"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    python = venv / "bin/python"
    site = subprocess.check_output(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        text=True,
    ).strip()
    shutil.copytree(ROOT / "src/loopzero", Path(site) / "loopzero")
    monkeypatch.setattr(body_check.sys, "executable", str(python))
    source = (ROOT / "tests/fixtures/intelflo/pr_body_check.py").read_text(
        encoding="utf-8"
    )
    calls = []

    class BaseRunner:
        def run(self, args, *, check):
            calls.append(args)
            assert check is False
            return SimpleNamespace(returncode=0, stdout=source, stderr="")

    # Neither a same-named candidate checker nor a candidate package may run.
    candidate = tmp_path / "candidate"
    (candidate / "scripts/util").mkdir(parents=True)
    (candidate / "scripts/util/pr_body_check.py").write_text(
        "raise RuntimeError('candidate checker')"
    )
    (candidate / "loopzero").mkdir()
    (candidate / "loopzero/__init__.py").write_text(
        "raise RuntimeError('candidate package')"
    )
    monkeypatch.chdir(candidate)
    monkeypatch.setenv("PYTHONPATH", str(candidate))
    monkeypatch.delenv("INTELFLO_ROOT", raising=False)
    monkeypatch.delenv("LOOPZERO_ROOT", raising=False)
    # The real facade explicitly inserts both of its enclosing directories.
    # Its parent must not expose a shared/candidate-controlled temp root.
    (candidate / "html.py").write_text("raise RuntimeError('temporary root import')")
    monkeypatch.setattr(body_check.tempfile, "tempdir", str(candidate))

    body_check.validate_body_against_trusted_base(
        BaseRunner(), trusted_base_head="c" * 40, body=VALID_BODY, standalone=False
    )
    with pytest.raises(RuntimeError, match="Missing required '## Context and goal'"):
        body_check.validate_body_against_trusted_base(
            BaseRunner(), trusted_base_head="c" * 40, body="Closes #1", standalone=False
        )
    assert calls == [["git", "show", "c" * 40 + ":scripts/util/pr_body_check.py"]] * 2


@pytest.mark.parametrize(
    "failure", [None, subprocess.TimeoutExpired("checker", 60), OSError("spawn")]
)
def test_private_checker_files_are_removed_after_execution(failure):
    class BaseRunner:
        def run(self, args, *, check):
            return SimpleNamespace(returncode=0, stdout="trusted source", stderr="")

    paths = []

    def checker(argv):
        script, body = Path(argv[2]), Path(argv[4])
        paths.extend([script, body, script.parent, body.parent])
        assert script.name == "pr_body_check.py"
        assert script.parent.parent == body.parent
        assert script.parent.stat().st_mode & 0o777 == 0o700
        assert body.parent.stat().st_mode & 0o777 == 0o700
        assert script.read_text() == "trusted source"
        assert body.read_text() == VALID_BODY
        if failure is not None:
            raise failure
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def validate():
        body_check.validate_body_against_trusted_base(
            BaseRunner(),
            trusted_base_head="c" * 40,
            body=VALID_BODY,
            standalone=False,
            run_checker=checker,
        )

    if failure is None:
        validate()
    else:
        with pytest.raises(type(failure)):
            validate()
    assert paths and all(not path.exists() for path in paths)
