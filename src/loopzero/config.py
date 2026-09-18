"""Load `workflow.toml` into a frozen `Config`."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

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
    "delivery": {"merge": str, "reviewers": list},
}


class ConfigError(LoopZeroError):
    """The configuration file is missing, unreadable, or has invalid content."""


def load(path: Path | str) -> Config:
    """Parse `path` as workflow.toml and return a validated `Config`."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: config file not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return _build(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


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
    return Config(
        repo=name,
        base_branch=base,
        checks=_strings("checks.commands", checks.get("commands", [])),
        required_ci=_strings("checks.required_ci", checks.get("required_ci", [])),
        merge_strategy=merge,
        reviewers=reviewers,
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
