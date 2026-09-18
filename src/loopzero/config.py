"""Load `workflow.toml` into a frozen `Config`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .types import Config, LoopZeroError

MERGE_STRATEGIES = ("squash", "merge", "rebase")
REVIEWER_FAMILIES = ("claude", "codex")
# Host locations that must never be exposed to the sandbox, even read-only, because they
# hold credentials or live sockets (docker, ssh-agent, gpg-agent, dbus). Subpaths of /home
# are allowed so tool caches such as ~/.local/bin can be listed explicitly.
FORBIDDEN_RO = ("/", "/home", "/root", "/run", "/var/run", "/proc", "/dev", "/sys")
FORBIDDEN_RO_TREES = ("/run", "/var/run", "/proc", "/dev", "/sys", "/root")

_SECTIONS: dict[str, dict[str, type]] = {
    "repo": {"name": str, "base": str},
    "checks": {
        "commands": list,
        "required_ci": list,
        "network": bool,
        "env_allowlist": list,
        "ro_paths": list,
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
        kwargs["sandbox_ro"] = _ro_paths(_strings("checks.ro_paths", checks["ro_paths"]))
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


def _ro_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    for raw in paths:
        if not raw.startswith("/"):
            raise ConfigError(f"checks.ro_paths entries must be absolute, got {raw!r}")
        path = Path(raw).resolve()
        if str(path) in FORBIDDEN_RO or any(
            path.is_relative_to(tree) for tree in FORBIDDEN_RO_TREES
        ):
            raise ConfigError(f"checks.ro_paths must not expose {raw!r} (host sockets or secrets)")
    return paths
