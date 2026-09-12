"""Probe real host boundaries once; an unavailable namespace is never a pass."""
import shutil

from loopzero.kernel.capabilities import probe_bwrap

_bwrap = shutil.which("bwrap")
_probe = probe_bwrap(_bwrap) if _bwrap else None
NAMESPACE_AVAILABLE = _probe is not None and _probe.returncode == 0
if _probe is None:
    NAMESPACE_REASON = "bubblewrap capability probe unavailable: bwrap not installed"
elif _probe.returncode != 0:
    NAMESPACE_REASON = (
        "bubblewrap capability probe failed: "
        + (_probe.stderr.strip() or f"exit {_probe.returncode}")
    )
else:
    NAMESPACE_REASON = ""
