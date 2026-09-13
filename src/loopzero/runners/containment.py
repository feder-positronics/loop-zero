"""Positive environment containment for runtime children.

Filesystem sandbox policy belongs to :mod:`loopzero.kernel`. Runner launches
accept an opaque caller-supplied argv wrapper through ``runners.process`` and
do not construct mounts or reason about Git authority here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .settings import RuntimeSettings, get_settings


_ALWAYS_REMOVED = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"})


def _conveys_parent_authority(name: str) -> bool:
    normalized = name.casefold()
    return name in _ALWAYS_REMOVED or "lease" in normalized or "nonce" in normalized


def worker_child_environment(
    base: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
    settings: RuntimeSettings | None = None,
) -> dict[str, str]:
    """Build a worker/probe environment without parent publication authority.

    Only exact names in :attr:`RuntimeSettings.child_env_allowlist` and locale
    category variables (``LC_*``) survive.  Lease/nonce names and common Git or
    SSH publication credentials are denied even if a consumer adds them to the
    allowlist.  ``extra`` is merged before filtering and therefore grants no
    bypass.
    """
    active = settings or get_settings()
    candidate = dict(os.environ if base is None else base)
    if extra is not None:
        candidate.update(extra)
    environment = {
        name: value
        for name, value in candidate.items()
        if (name in active.child_env_allowlist or name.startswith("LC_"))
        and not _conveys_parent_authority(name)
    }
    return environment


__all__ = [
    "worker_child_environment",
]
