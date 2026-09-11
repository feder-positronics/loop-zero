"""Carry installed-package identity into explicitly isolated CLI test envs."""
import os
import sys

def package_environment(environment):
    return {**(os.environ if environment is None else environment),
            "LOOPZERO_PYTHON": sys.executable, "LOOPZERO_ENV_PREFIX": "INTELFLO"}
