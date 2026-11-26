"""loop-zero v2: minimal delivery toolkit."""

__version__ = "2.0.0a0"


def installed_version() -> str:
    """Package version plus the pinned source revision when installed from Git."""
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution("loopzero")
    except PackageNotFoundError:
        return __version__
    try:
        commit = json.loads(dist.read_text("direct_url.json") or "{}")["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        commit = "unknown revision (not installed from a Git URL)"
    return f"{dist.version} {commit}"
