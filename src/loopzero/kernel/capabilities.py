"""Executed probes for kernel containment capabilities."""

from __future__ import annotations

import subprocess


def bwrap_probe_command(bwrap: str) -> list[str]:
    """Exercise the exact chmod/perms and remount-ro surface we depend on."""
    return [
        bwrap, "--die-with-parent", "--new-session", "--unshare-user",
        "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/",
        "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--perms", "0555", "--dir", "/tmp/loopzero-capability",
        "--remount-ro", "/tmp", "--", "/bin/sh", "-c",
        "test \"$(/usr/bin/stat -c %a /tmp/loopzero-capability)\" = 555 && "
        "! /usr/bin/chmod 0700 /tmp/loopzero-capability 2>/dev/null && "
        "test \"$(/usr/bin/stat -c %a /tmp/loopzero-capability)\" = 555 && "
        "/usr/bin/true",
    ]


def probe_bwrap(bwrap: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        bwrap_probe_command(bwrap), capture_output=True, text=True,
        check=False, timeout=15,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )


def require_bwrap_capability(bwrap: str) -> None:
    try:
        result = probe_bwrap(bwrap)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("bound job supervision capability probe could not run") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(
            "bound job supervision requires working chmod/perms and remount-ro: "
            + detail
        )
