"""Carry installed-package identity into explicitly isolated CLI test envs."""
import atexit
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER_ROOT = Path(tempfile.gettempdir()) / f"loopzero-package-python-{os.getpid()}"
_WRAPPER_ROOT.mkdir(mode=0o700, exist_ok=True)
atexit.register(shutil.rmtree, _WRAPPER_ROOT, ignore_errors=True)
PACKAGE_PYTHON = _WRAPPER_ROOT / "python"
PACKAGE_PYTHON.write_text(
    "#!/bin/sh\n"
    f"export PYTHONPATH={shlex.quote(str(PACKAGE_ROOT / 'src'))}\n"
    f"exec {shlex.quote(sys.executable)} \"$@\"\n",
    encoding="utf-8",
)
PACKAGE_PYTHON.chmod(0o700)

def package_environment(environment):
    selected = dict(os.environ if environment is None else environment)
    # One startup-hardening regression intentionally supplies a hostile
    # PYTHONPATH to its control process. Every real kernel subprocess is pinned
    # to the source under test, independent of stale editable installations.
    if "INTELFLO_TEST_PYTHONPATH_MARKER" not in selected:
        selected["PYTHONPATH"] = str(PACKAGE_ROOT / "src")
    selected.update({
        "LOOPZERO_PYTHON": str(PACKAGE_PYTHON),
        "LOOPZERO_TEST_PYTHON": sys.executable,
        "LOOPZERO_ENV_PREFIX": "INTELFLO",
    })
    return selected
