"""Carry installed-package identity into explicitly isolated CLI test envs."""
import os
import sys
from pathlib import Path

PACKAGE_PYTHON = Path(__file__).with_name("package-python")
PACKAGE_ROOT = Path(__file__).resolve().parents[3]

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
