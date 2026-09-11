"""Probe real host boundaries once; an unavailable namespace is never a pass."""
import os
import shutil
import subprocess
from pathlib import Path

_bwrap = shutil.which("bwrap")
_probe = subprocess.run([_bwrap, "--ro-bind", "/", "/", "--unshare-user", "--", "/bin/true"], capture_output=True, text=True) if _bwrap else None
NAMESPACE_AVAILABLE = _probe is not None and _probe.returncode == 0
NAMESPACE_REASON = "bubblewrap cannot create a namespace: " + (_probe.stderr.strip() if _probe else "bwrap not installed")
HOST_STATE_WRITABLE = os.access(Path.home() / ".local/state", os.W_OK)
