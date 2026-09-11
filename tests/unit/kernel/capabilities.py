"""Probe real host boundaries once; an unavailable namespace is never a pass."""
import shutil
import subprocess

_bwrap = shutil.which("bwrap")
_probe = subprocess.run(
    [
        _bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--ro-bind",
        "/usr",
        "/usr",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--",
        "/usr/bin/true",
    ],
    capture_output=True,
    text=True,
) if _bwrap else None
_help = subprocess.run(
    [_bwrap, "--help"], capture_output=True, text=True
) if _bwrap else None
_required_surface = (
    _help is not None
    and "--perms" in (_help.stdout + _help.stderr)
    and "--remount-ro" in (_help.stdout + _help.stderr)
)
NAMESPACE_AVAILABLE = (
    _probe is not None and _probe.returncode == 0 and _required_surface
)
if _probe is None:
    NAMESPACE_REASON = "bubblewrap cannot create a namespace: bwrap not installed"
elif _probe.returncode != 0:
    NAMESPACE_REASON = "bubblewrap cannot create a namespace: " + _probe.stderr.strip()
else:
    NAMESPACE_REASON = "bubblewrap lacks required --perms/--remount-ro support"
