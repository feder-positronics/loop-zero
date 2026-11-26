"""Load `workflow.toml` into a frozen `Config`."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import _proc
from . import worktree as worktree_mod
from .types import Config, LoopZeroError, ResourceLimits, ReviewConfig

MERGE_STRATEGIES = ("squash", "merge", "rebase", "queue", "mergify")
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
        "commands": list, "runner": list, "inputs": dict, "required_ci": list, "network": bool,
        "env_allowlist": list, "ro_paths": list, "writable": list, "scratch": list,
        "env": dict, "limits": dict,
    },
    "delivery": {
        "merge": str, "mergify_queue": str,
        "reviewers": list, "review_publishers": list, "reviewer_ro_paths": list,
        "review_chunk_bytes": int, "review": dict,
    },
}


class ConfigError(LoopZeroError):
    """The configuration file is missing, unreadable, or has invalid content."""


def load(path: Path | str) -> Config:
    """Load and validate a workflow file."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path}: config file not found. Check out the intended repository, "
            "configure loop-zero for this repository "
            "(https://github.com/feder-positronics/loop-zero/blob/main/SETUP.md#configure), "
            "or follow this repository's review process if it has one."
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return _build(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def run_git(worktree: Path, *args: str, timeout: float = 60) -> _proc.Completed:
    return _proc.run(["git", *args], cwd=worktree, env_allowlist=worktree_mod.GIT_ENV, timeout=timeout)


def base_revision(worktree: Path, base_branch: str) -> str:
    """Resolve the remote base, or the task's recorded base when the ref is absent."""
    done = run_git(worktree, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}")
    if done.exit_code == 0:
        return done.stdout.strip()
    if done.exit_code == 1:
        return worktree_mod.base_sha(worktree)
    message = _proc.tail(done.stderr or done.stdout, 1)
    raise ConfigError(f"git rev-parse origin/{base_branch} failed: {message}")


def _has_replacement_refs(common_dir: Path) -> bool:
    # A replacement can change what Git reads at a fixed object name.
    for entry in ("refs/replace", "reftable"):
        try:
            (common_dir / entry).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    try:
        return b" refs/replace/" in (common_dir / "packed-refs").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _remote_workflow(worktree: Path, branch: str, previous: Config | None, *,
                     refresh: bool, read_timeout: Callable[[float], float]) -> tuple[Config, str]:
    source = f"origin/{branch}:workflow.toml"
    hint = "Fetch the trusted base workflow, or supply --config for bootstrap or an override."
    ref = f"refs/remotes/origin/{branch}"
    if refresh:
        fetched = run_git(worktree, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
                          "--no-write-fetch-head", "origin", f"+refs/heads/{branch}:{ref}", timeout=read_timeout(60))
        if fetched.exit_code != 0:
            raise ConfigError(f"could not refresh origin/{branch}. {hint}")
    directory_args = ["--path-format=absolute", "--git-common-dir"] if previous is not None else []
    resolved = _proc.run_git_paths(
        ["git", "rev-parse", *directory_args, "--verify", f"{ref}^{{commit}}"],
        cwd=worktree, env_allowlist=worktree_mod.GIT_ENV, timeout=read_timeout(60),
    )
    if resolved.exit_code != 0:
        raise ConfigError(f"{source} has no remote-tracking base commit. {hint}")
    common_dir, _, revision = resolved.stdout.rpartition("\n")
    if (previous is not None and previous.base_branch == branch and previous.trusted_base_revision == revision
            and common_dir and not _has_replacement_refs(Path(common_dir))):
        return previous, revision
    shown = _proc.run(
        ["git", "show", f"{revision}:workflow.toml"],
        cwd=worktree, env_allowlist=worktree_mod.GIT_ENV, timeout=read_timeout(60), preserve_newlines=True, encoding="utf-8",
    )
    if shown.exit_code != 0:
        raise ConfigError(f"cannot read {source}. {hint}")
    try:
        text = shown.stdout.encode("utf-8", errors="surrogateescape").decode("utf-8")
        return _build(tomllib.loads(text)), revision
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}. {hint}") from None
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}. {hint}") from None


def load_trusted(worktree: Path, *, previous: Config | None = None, refresh: bool = False,
                 read_timeout: Callable[[float], float] = float) -> Config:
    """Select delivery policy using Git's origin default and its trusted workflow."""
    prefix = "refs/remotes/origin/"
    default = run_git(worktree, "symbolic-ref", "--quiet", f"{prefix}HEAD", timeout=read_timeout(60))
    ref = default.stdout.strip()
    if default.exit_code != 0 or not ref.startswith(prefix):
        raise ConfigError(
            "cannot resolve the trusted origin default branch. Run git remote set-head origin --auto, "
            "or supply --config for bootstrap or an override."
        )
    branch = ref.removeprefix(prefix)
    policy, revision = _remote_workflow(worktree, branch, previous, refresh=refresh, read_timeout=read_timeout)
    if policy.base_branch != branch:
        branch = policy.base_branch
        policy, revision = _remote_workflow(worktree, branch, previous, refresh=refresh, read_timeout=read_timeout)
        if policy.base_branch != branch:
            raise ConfigError("trusted origin workflows disagree on repo.base; align them or supply --config")
    return replace(policy, trusted_base_revision=revision)


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
    merge, mergify_queue = _merge_settings(delivery)
    reviewers = _strings("delivery.reviewers", delivery.get("reviewers", list(REVIEWER_FAMILIES)))
    if not reviewers:
        raise ConfigError("delivery.reviewers must list at least one reviewer")
    for reviewer in reviewers:
        if reviewer not in REVIEWER_FAMILIES:
            raise ConfigError(
                f"delivery.reviewers entries must be in {REVIEWER_FAMILIES}, got {reviewer!r}"
            )
    review_chunk_bytes = delivery.get("review_chunk_bytes", 200_000)
    if isinstance(review_chunk_bytes, bool) or review_chunk_bytes < 4096:
        raise ConfigError("delivery.review_chunk_bytes must be an int >= 4096")
    review: dict[str, ReviewConfig] = {}
    for family, table in delivery.get("review", {}).items():
        if family not in REVIEWER_FAMILIES:
            raise ConfigError(f"unknown review family delivery.review.{family}")
        if not isinstance(table, dict):
            raise ConfigError(f"[delivery.review.{family}] must be a table")
        label = f"delivery.review.{family}"
        _reject_unknown(f"[{label}]", table, ("model", "effort", "allowed_efforts"))
        for key in ("model", "effort"):
            if key in table and (not isinstance(table[key], str) or not table[key]):
                raise ConfigError(f"{label}.{key} must be a non-empty str")
        allowed = table.get("allowed_efforts", [])
        if not isinstance(allowed, list):
            raise ConfigError(f"{label}.allowed_efforts must be a list")
        review[family] = ReviewConfig(table.get("model"), table.get("effort"),
                                      _strings(f"{label}.allowed_efforts", allowed))

    commands = _strings("checks.commands", checks.get("commands", []))
    if any(not command.strip() for command in commands):
        raise ConfigError("checks.commands entries must each be a nonblank command")
    kwargs: dict[str, Any] = {}
    if "runner" in checks:
        runner = _strings("checks.runner", checks["runner"])
        if not runner or any(not arg or "\0" in arg for arg in runner):
            raise ConfigError("checks.runner must be a nonempty argv prefix")
        if "/" in runner[0] and not Path(runner[0]).is_absolute():
            raise ConfigError("checks.runner executable paths must be absolute")
        kwargs["check_runner"] = runner
    inputs = {}
    for key, value in checks.get("inputs", {}).items():
        label = f'checks.inputs."{key}"'
        if key != "*" and key not in commands:
            raise ConfigError(f"{label} must exactly match a checks.commands entry")
        inputs[key] = _strings(label, value)
    for key, field, read in (("env_allowlist", "env_allowlist", _strings),
                             ("ro_paths", "sandbox_ro", _host_paths),
                             ("writable", "writable", _host_paths)):
        if key in checks:
            kwargs[field] = read(f"checks.{key}", checks[key])
    env = _env_table(checks.get("env", {}))
    if "scratch" in checks:
        kwargs["scratch"] = _strings("checks.scratch", checks["scratch"])
        for entry in kwargs["scratch"]:
            parts = Path(entry).parts
            if not parts or entry.startswith("/") or ".." in parts or parts[0] == ".git":
                raise ConfigError(f"checks.scratch entries must be relative, got {entry!r}")
    if "limits" in checks:
        kwargs["limits"] = _limits(checks["limits"])
        if "runner" in checks and checks["limits"]:
            keys = ", ".join(f"checks.limits.{key}" for key in sorted(checks["limits"]))
            raise ConfigError(f"{keys} apply only to local checks and cannot be set with checks.runner")
    reviewer_ro_paths = _host_paths(
        "delivery.reviewer_ro_paths", delivery.get("reviewer_ro_paths", [])
    )
    return Config(
        repo=name,
        base_branch=base,
        checks=commands,
        required_ci=_strings("checks.required_ci", checks.get("required_ci", [])),
        merge_strategy=merge,
        mergify_queue=mergify_queue,
        reviewers=reviewers,
        review_publishers=_strings("delivery.review_publishers",
                                   delivery.get("review_publishers", [])),
        review=review,
        reviewer_ro_paths=reviewer_ro_paths,
        review_chunk_bytes=review_chunk_bytes,
        network=checks.get("network", False),
        inputs=inputs, env=env, **kwargs,
    )


def _merge_settings(delivery: dict[str, Any]) -> tuple[str, str | None]:
    merge = delivery.get("merge", "squash")
    if merge not in MERGE_STRATEGIES:
        raise ConfigError(f"delivery.merge must be one of {MERGE_STRATEGIES}, got {merge!r}")
    mergify_queue = delivery.get("mergify_queue")
    if merge == "mergify" and not mergify_queue:
        raise ConfigError("delivery.mergify_queue is required when delivery.merge = 'mergify'")
    if mergify_queue is not None and merge != "mergify":
        raise ConfigError(
            f"delivery.mergify_queue requires delivery.merge = 'mergify', got {merge!r}"
        )
    if mergify_queue is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", mergify_queue):
        raise ConfigError("delivery.mergify_queue must be a non-empty command-safe name")
    return merge, mergify_queue


def _reject_unknown(where: str, table: dict[str, Any], allowed: Any) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _strings(label: str, value: list[Any]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
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
    return tuple(sorted(table.items()))


def _limits(table: dict[str, Any]) -> ResourceLimits:
    defaults = ResourceLimits()
    _reject_unknown("checks.limits", table, defaults.__dict__)
    values = defaults.__dict__ | table
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"checks.limits.{name} must be a positive int")
    return ResourceLimits(**values)
