"""Load `workflow.toml` into a frozen `Config`."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from . import _proc
from . import worktree as worktree_mod
from .types import Config, LoopZeroError, ResourceLimits

MERGE_STRATEGIES = ("squash", "merge", "rebase")
REVIEWER_FAMILIES = ("claude", "codex")
# Host locations that must never be exposed to the sandbox, even read-only, because they
# hold credentials or live sockets (docker, ssh-agent, gpg-agent, dbus). Subpaths of /home
# are allowed so tool caches such as ~/.local/bin can be listed explicitly.
FORBIDDEN_RO = ("/", "/home", "/root", "/run", "/var/run", "/proc", "/dev", "/sys")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
FORBIDDEN_RO_TREES = ("/run", "/var/run", "/proc", "/dev", "/sys", "/root")

_SECTIONS: dict[str, dict[str, type]] = {
    "repo": {"name": str, "base": str},
    "checks": {
        "commands": list,
        "required_ci": list,
        "network": bool,
        "env_allowlist": list,
        "ro_paths": list,
        "writable": list,
        "scratch": list,
        "env": dict,
        "limits": dict,
    },
    "delivery": {"merge": str, "reviewers": list, "reviewer_ro_paths": list},
}


class ConfigError(LoopZeroError):
    """The configuration file is missing, unreadable, or has invalid content."""


def load(path: Path | str, *, checks: dict[str, Any] | None = None) -> Config:
    """Parse `path` as workflow.toml and return a validated `Config`."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: config file not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    if checks is not None:
        data["checks"] = checks
    try:
        return _build(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _base_source(worktree: Path, base_branch: str) -> tuple[str, str]:
    done = _proc.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}"],
        cwd=worktree, env_allowlist=worktree_mod.GIT_ENV, timeout=60,
    )
    if done.exit_code == 0:
        return f"origin/{base_branch}", done.stdout.strip()
    if done.exit_code == 1:
        revision = worktree_mod.base_sha(worktree)
        return revision, revision
    message = _proc.tail(done.stderr or done.stdout, 1)
    raise ConfigError(f"git rev-parse origin/{base_branch} failed: {message}")


def base_revision(worktree: Path, base_branch: str) -> str:
    """Resolve the remote base, or the task's recorded base when the ref is absent."""
    return _base_source(worktree, base_branch)[1]


def load_base(worktree: Path, base_branch: str) -> dict[str, Any] | None:
    """Read the base revision's `[checks]`, or None when it has no workflow file."""
    base, revision = _base_source(worktree, base_branch)
    shown = _proc.run(
        ["git", "show", f"{base}:workflow.toml"],
        cwd=worktree, env_allowlist=worktree_mod.GIT_ENV, timeout=60,
    )
    if shown.exit_code != 0:
        listed = _proc.run(
            ["git", "ls-tree", revision, "--", "workflow.toml"],
            cwd=worktree, env_allowlist=worktree_mod.GIT_ENV, timeout=60,
        )
        if listed.exit_code == 0 and not listed.stdout.strip():
            return None
        message = _proc.tail(shown.stderr or shown.stdout, 1)
        raise ConfigError(f"git show origin/{base_branch}:workflow.toml failed: {message}")
    try:
        data = tomllib.loads(shown.stdout)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"origin/{base_branch}:workflow.toml: invalid TOML: {exc}") from exc
    checks = data.get("checks", {})
    if not isinstance(checks, dict):
        raise ConfigError(f"origin/{base_branch}:workflow.toml: [checks] must be a table")
    return checks


def _build(data: dict[str, Any]) -> Config:
    _reject_unknown("top level", data, _SECTIONS)
    for section, keys in _SECTIONS.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise ConfigError(f"[{section}] must be a table")
        _reject_unknown(f"[{section}]", table, keys)
        for key, kind in keys.items():
            if key in table and not isinstance(table[key], kind):
                raise ConfigError(f"{section}.{key} must be a {kind.__name__}")

    repo = data.get("repo", {})
    checks = data.get("checks", {})
    delivery = data.get("delivery", {})

    if "name" not in repo:
        raise ConfigError("missing required key repo.name (\"owner/name\")")
    name = repo["name"]
    if name.count("/") != 1 or not all(name.split("/")):
        raise ConfigError(f'repo.name must look like "owner/name", got {name!r}')

    base = repo.get("base", "main")
    if not base:
        raise ConfigError("repo.base must not be empty")
    merge = delivery.get("merge", "squash")
    if merge not in MERGE_STRATEGIES:
        raise ConfigError(f"delivery.merge must be one of {MERGE_STRATEGIES}, got {merge!r}")
    reviewers = _strings("delivery.reviewers", delivery.get("reviewers", list(REVIEWER_FAMILIES)))
    if not reviewers:
        raise ConfigError("delivery.reviewers must list at least one reviewer")
    for reviewer in reviewers:
        if reviewer not in REVIEWER_FAMILIES:
            raise ConfigError(
                f"delivery.reviewers entries must be in {REVIEWER_FAMILIES}, got {reviewer!r}"
            )

    kwargs: dict[str, Any] = {}
    if "env_allowlist" in checks:
        kwargs["env_allowlist"] = _strings("checks.env_allowlist", checks["env_allowlist"])
    if "ro_paths" in checks:
        kwargs["sandbox_ro"] = _host_paths("checks.ro_paths", checks["ro_paths"])
    if "writable" in checks:
        kwargs["writable"] = _host_paths("checks.writable", checks["writable"])
    if "env" in checks:
        kwargs["env"] = _env_table(checks["env"])
    if "scratch" in checks:
        kwargs["scratch"] = _strings("checks.scratch", checks["scratch"])
        for entry in kwargs["scratch"]:
            parts = Path(entry).parts
            if entry in ("", ".") or entry.startswith("/") or ".." in parts or parts[0] == ".git":
                raise ConfigError(f"checks.scratch entries must be relative, got {entry!r}")
    if "limits" in checks:
        kwargs["limits"] = _limits(checks["limits"])
    reviewer_ro_paths = _host_paths(
        "delivery.reviewer_ro_paths", delivery.get("reviewer_ro_paths", [])
    )
    return Config(
        repo=name,
        base_branch=base,
        checks=_strings("checks.commands", checks.get("commands", [])),
        required_ci=_strings("checks.required_ci", checks.get("required_ci", [])),
        merge_strategy=merge,
        reviewers=reviewers,
        reviewer_ro_paths=reviewer_ro_paths,
        network=checks.get("network", False),
        **kwargs,
    )


def _reject_unknown(where: str, table: dict[str, Any], allowed: Any) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _strings(label: str, value: list[Any]) -> tuple[str, ...]:
    if not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _host_paths(label: str, value: list[Any]) -> tuple[str, ...]:
    paths = _strings(label, value)
    for raw in paths:
        if not raw.startswith("/"):
            raise ConfigError(f"{label} entries must be absolute, got {raw!r}")
        path = Path(raw).resolve()
        if str(path) in FORBIDDEN_RO or any(
            path.is_relative_to(tree) for tree in FORBIDDEN_RO_TREES
        ):
            raise ConfigError(f"{label} must not expose {raw!r} (host sockets or secrets)")
    return paths


def _env_table(table: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    for name, value in table.items():
        if not _ENV_NAME.match(name):
            raise ConfigError(f"checks.env has invalid variable name {name!r}")
        if name in ("PATH", "HOME"):
            raise ConfigError(f"checks.env must not override {name}")
        if not isinstance(value, str):
            raise ConfigError(f"checks.env.{name} must be a str")
    return tuple(table.items())


def _limits(table: dict[str, Any]) -> ResourceLimits:
    defaults = ResourceLimits()
    allowed = tuple(defaults.__dict__)
    _reject_unknown("checks.limits", table, allowed)
    values = defaults.__dict__ | table
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"checks.limits.{name} must be a positive int")
    return ResourceLimits(**values)
