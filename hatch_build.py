"""Limit checkout source distributions to files tracked by Git."""

import os
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class TrackedSdistFiles(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        if not (root / ".git").exists():
            return

        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        self.build_config.set_exclude_all()
        build_data["force_include"].update(
            (name, name)
            for name in os.fsdecode(tracked).split("\0")
            if name and (root / name).is_file()
        )
