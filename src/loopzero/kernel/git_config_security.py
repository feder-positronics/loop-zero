#!/usr/bin/env python3
"""Digest security-relevant local Git config while ignoring branch bookkeeping."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from subprocess import run as run_command

TRUSTED_GIT = "/usr/bin/git"


def parse_git_config_entries(raw_config: bytes) -> tuple[tuple[str, str | None], ...]:
    """Parse one local config with Git's grammar and without following includes."""

    completed = run_command(
        [TRUSTED_GIT, "config", "--file", "-", "--no-includes", "--null", "--list"],
        input=raw_config,
        capture_output=True,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
        timeout=5,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git configuration cannot be parsed: {detail or 'unknown error'}")
    entries: list[tuple[str, str | None]] = []
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        raw_key, separator, raw_value = record.partition(b"\n")
        try:
            key = raw_key.decode("utf-8")
            value = raw_value.decode("utf-8") if separator else None
        except UnicodeDecodeError as exc:
            raise ValueError("Git configuration is not valid UTF-8") from exc
        entries.append((key, value))
    return tuple(entries)


_INERT_REMOTE_KEYS = frozenset(
    {
        "url",
        "pushurl",
        "fetch",
        "push",
        "mirror",
        "tagopt",
        "prune",
        "prunetags",
        "skipdefaultupdate",
        "skipfetchall",
        "gh-resolved",
    }
)
# `<helper>::<address>` selects a remote helper; `ext::` runs an arbitrary
# command and `fd::` reads from launcher descriptors. Ordinary schemes such as
# https://, ssh://, git@host:path, file:// and plain paths never contain "::"
# before the address.
_REMOTE_HELPER_URL = re.compile(r"[A-Za-z0-9+.\-]*::")


def git_config_key_can_redirect(name: str) -> bool:
    normalized = name.casefold()
    if normalized.startswith(
        (
            "credential.",
            "http.",
            "https.",
            "include.",
            "includeif.",
            "url.",
            "protocol.",
            "diff.",
            "filter.",
            "alias.",
            "gpg.",
        )
    ):
        return True
    if normalized.startswith("merge.") and normalized.endswith(".driver"):
        return True
    if normalized in {
        "extensions.partialclone",
        "extensions.worktreeconfig",
        "core.askpass",
        "core.editor",
        "core.fsmonitor",
        "core.gitproxy",
        "core.hookspath",
        "core.pager",
        "core.alternaterefscommand",
        "core.sshcommand",
        "core.worktree",
    }:
        return True
    remote = re.fullmatch(r"remote\.(.+)\.([^.]+)", normalized)
    if remote is not None:
        # Any remote (origin or a second one) keeps only inert bookkeeping:
        # URLs, refspecs, mirror and prune flags. `vcs`, `proxy`, `uploadpack`,
        # `receivepack`, `promisor` and `partialclonefilter` select helpers,
        # commands, or lazy fetching and are rejected for every remote.
        return remote.group(2) not in _INERT_REMOTE_KEYS
    return False


def git_config_value_can_redirect(name: str, value: str | None) -> bool:
    """Reject transport-redirecting remote URLs such as ``ext::`` and ``fd::``."""
    normalized = name.casefold()
    if value is None:
        return False
    if re.fullmatch(r"remote\..+\.(?:url|pushurl)", normalized):
        return _REMOTE_HELPER_URL.match(value) is not None
    return False


def validated_git_config_entries(
    raw_config: bytes,
) -> tuple[tuple[str, str | None], ...]:
    entries = parse_git_config_entries(raw_config)
    if (
        any(git_config_key_can_redirect(key) for key, _value in entries)
        or any(git_config_value_can_redirect(key, value) for key, value in entries)
        or any(
            key.casefold() == "core.repositoryformatversion" and value != "0"
            for key, value in entries
        )
    ):
        raise ValueError(
            "Git configuration uses unmeasured execution, redirect, include, "
            "URL rewrite, or worktree configuration"
        )
    return entries


def origin_url(raw_config: bytes) -> str:
    urls = [
        value
        for key, value in validated_git_config_entries(raw_config)
        if key.casefold() == "remote.origin.url" and value is not None
    ]
    if len(urls) != 1:
        raise ValueError("Git configuration must contain exactly one origin URL")
    return urls[0]


def security_projection_sha256(raw_config: bytes) -> str:
    """Hash every local-config value except inert per-branch bookkeeping.

    Linked worktrees share the common repository config. Normal branch creation,
    tracking, and cleanup therefore rewrite ``branch.*`` entries while a closeout
    waits for hosted CI. Network commands in the closeout path use an explicit
    canonical URL and repository binding, so those entries cannot redirect them.
    All other sections remain pinned byte-for-value after Git-config parsing.
    """

    projection = [
        (key, value)
        for key, value in parse_git_config_entries(raw_config)
        if not key.casefold().startswith("branch.")
    ]
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--origin-url",
        action="store_true",
        help="Print the single validated origin URL instead of the projection digest.",
    )
    args = parser.parse_args(argv)
    try:
        if args.config.is_symlink() or not args.config.is_file():
            raise ValueError("Git configuration is not one regular file")
        raw_config = args.config.read_bytes()
        validated_git_config_entries(raw_config)
        print(
            origin_url(raw_config)
            if args.origin_url
            else security_projection_sha256(raw_config)
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
