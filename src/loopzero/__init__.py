"""Executable mechanisms of the loop-zero shared agent workflow.

The package is installed from the same commit SHA as the vendored ``core/``
snapshot. ``__version__`` is the package's own label; ``loopzero status``
compares it with the snapshot's ``core/VERSION`` in the consumer.
"""

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("loopzero")
except PackageNotFoundError:  # running from a source checkout without install
    from pathlib import Path as _Path

    _candidate = _Path(__file__).resolve().parents[2] / "core" / "VERSION"
    __version__ = _candidate.read_text().strip() if _candidate.exists() else "0+unknown"

__all__ = ["__version__"]
