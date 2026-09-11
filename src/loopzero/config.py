"""Load and validate a consumer's ``workflow.toml``.

The file is read by humans, agents and this package. The package executes
only ``[hooks]`` commands, and privileged hooks are read from the approved
base revision through :func:`hooks_from_base`, never from the candidate
worktree. Validation reports every problem at once so a consumer fixes the
file in one pass.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKFLOW_FILE = "workflow.toml"
CONTRACT_ID = "loop-zero-v1"
PROFILES = ("python", "fastapi", "nextjs", "angular")
RUNNERS = ("claude", "codex", "cursor", "acp", "fake")
SANDBOXES = ("bwrap",)
CHECK_GROUPS = ("required", "advisory", "scheduled")
HOOK_NAMES = ("worktree_setup", "acceptance", "db_acceptance", "closeout")
PRIVILEGED_HOOKS = ("acceptance", "db_acceptance", "closeout")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ConfigError(ValueError):
    """Raised with every validation problem joined by newlines."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Alias:
    runner: str
    model: str
    write: bool = False
    agent: str | None = None


@dataclass(frozen=True)
class Tier:
    alias: str
    effort: str = "medium"
    read_only: bool = False


@dataclass(frozen=True)
class Profile:
    """Validated view of ``workflow.toml``. Paths are relative to ``root``."""

    root: Path
    core_repository: str
    core_revision: str
    core_path: Path
    profiles: tuple[str, ...]
    checks: dict[str, tuple[str, ...]]
    env_prefix: str = "LOOPZERO"
    audit_root: Path = Path(".audit")
    state_root: str = "~/.local/state/loopzero"
    state_root_explicit: bool = False
    contract: str = CONTRACT_ID
    epoch: int = 1
    sandbox: str = "bwrap"
    toolchain: dict[str, Any] = field(default_factory=dict)
    aliases: dict[str, Alias] = field(default_factory=dict)
    tiers: dict[str, Tier] = field(default_factory=dict)
    max_reviews_per_pr: int = 1
    max_delta_reviews: int = 1
    required_sections: tuple[str, ...] = ()
    native_protection: bool = True
    path_classes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def snapshot_dir(self) -> Path:
        return self.root / self.core_path

    def snapshot_version(self) -> str | None:
        version_file = self.snapshot_dir / "VERSION"
        if not version_file.is_file():
            return None
        return version_file.read_text(encoding="utf-8").strip()


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError([f"{path}: missing"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path}: invalid TOML: {exc}"]) from None


def _commands(value: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else _fail(problems, f"{where}: empty command")
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return tuple(value)
    return _fail(problems, f"{where}: must be a string or a list of nonempty strings")


def _fail(problems: list[str], message: str) -> tuple[str, ...]:
    problems.append(message)
    return ()


def validate(data: dict[str, Any], root: Path) -> Profile:
    """Return a :class:`Profile` or raise :class:`ConfigError` listing every problem."""
    problems: list[str] = []

    core = data.get("core")
    if not isinstance(core, dict):
        problems.append("[core]: missing table")
        core = {}
    repository = core.get("repository")
    revision = core.get("revision")
    core_path = core.get("path")
    if not isinstance(repository, str) or not repository.strip():
        problems.append("[core].repository: must be a nonempty string")
    if not isinstance(revision, str) or not _SHA_RE.match(revision):
        problems.append("[core].revision: must be a full 40-character lowercase commit SHA")
    if not isinstance(core_path, str) or not core_path.strip() or Path(core_path).is_absolute():
        problems.append("[core].path: must be a relative path to the vendored snapshot")

    profiles = data.get("profiles", [])
    if not isinstance(profiles, list) or any(p not in PROFILES for p in profiles):
        problems.append(f"profiles: must be a list drawn from {', '.join(PROFILES)}")
        profiles = []

    checks_table = data.get("checks")
    checks: dict[str, tuple[str, ...]] = {}
    if not isinstance(checks_table, dict):
        problems.append("[checks]: missing table with required, advisory and scheduled arrays")
    else:
        seen: set[str] = set()
        for group in CHECK_GROUPS:
            names = checks_table.get(group)
            if not isinstance(names, list):
                problems.append(f"[checks].{group}: must be an array of exact check names")
                continue
            for name in names:
                if not isinstance(name, str) or not name.strip():
                    problems.append(f"[checks].{group}: check names must be nonempty strings")
                elif name in seen:
                    problems.append(f"[checks]: {name!r} appears in more than one group")
                seen.add(name if isinstance(name, str) else "")
            checks[group] = tuple(n for n in names if isinstance(n, str))

    package = data.get("package", {})
    if not isinstance(package, dict):
        problems.append("[package]: must be a table")
        package = {}
    env_prefix = package.get("env_prefix", "LOOPZERO")
    if not isinstance(env_prefix, str) or not _PREFIX_RE.match(env_prefix):
        problems.append("[package].env_prefix: must match [A-Z][A-Z0-9_]*")
    audit_root = package.get("audit_root", ".audit")
    if not isinstance(audit_root, str) or Path(audit_root).is_absolute():
        problems.append("[package].audit_root: must be a relative path")
    state_root_explicit = "state_root" in package
    state_root = package.get("state_root", "~/.local/state/loopzero")
    if not isinstance(state_root, str) or not state_root:
        problems.append("[package].state_root: must be a nonempty path")
    contract = package.get("contract", CONTRACT_ID)
    if contract != CONTRACT_ID:
        problems.append(f"[package].contract: must be {CONTRACT_ID!r}")
    epoch = package.get("epoch", 1)
    if type(epoch) is not int or epoch < 1:
        problems.append("[package].epoch: must be a positive integer")
    sandbox = package.get("sandbox", "bwrap")
    if sandbox not in SANDBOXES:
        problems.append(f"[package].sandbox: must be one of {', '.join(SANDBOXES)}")

    toolchain = data.get("toolchain", {})
    if not isinstance(toolchain, dict):
        problems.append("[toolchain]: must be a table")
        toolchain = {}

    routing = data.get("routing", {})
    aliases: dict[str, Alias] = {}
    tiers: dict[str, Tier] = {}
    if not isinstance(routing, dict):
        problems.append("[routing]: must be a table")
    else:
        for name, spec in (routing.get("aliases") or {}).items():
            where = f"[routing.aliases].{name}"
            if not _ALIAS_RE.match(str(name)):
                problems.append(f"{where}: alias names match [a-z][a-z0-9-]*")
            if not isinstance(spec, dict) or spec.get("runner") not in RUNNERS:
                problems.append(f"{where}: runner must be one of {', '.join(RUNNERS)}")
                continue
            if not isinstance(spec.get("model"), str) or not spec["model"]:
                problems.append(f"{where}: model must be a nonempty string")
                continue
            aliases[name] = Alias(
                runner=spec["runner"],
                model=spec["model"],
                write=bool(spec.get("write", False)),
                agent=spec.get("agent"),
            )
        for name, spec in (routing.get("tiers") or {}).items():
            where = f"[routing.tiers].{name}"
            if not isinstance(spec, dict) or spec.get("alias") not in aliases:
                problems.append(f"{where}: alias must name an entry in [routing.aliases]")
                continue
            tiers[name] = Tier(
                alias=spec["alias"],
                effort=str(spec.get("effort", "medium")),
                read_only=bool(spec.get("read_only", False)),
            )

    review = data.get("review", {})
    if not isinstance(review, dict):
        problems.append("[review]: must be a table")
        review = {}
    max_reviews = review.get("max_reviews_per_pr", 1)
    max_delta = review.get("max_delta_reviews", 1)
    for label, value in (("max_reviews_per_pr", max_reviews), ("max_delta_reviews", max_delta)):
        if type(value) is not int or value < 0:
            problems.append(f"[review].{label}: must be a non-negative integer")
    sections = review.get("required_sections", [])
    if not isinstance(sections, list) or any(not isinstance(s, str) for s in sections):
        problems.append("[review].required_sections: must be a list of strings")
        sections = []

    github = data.get("github", {})
    if not isinstance(github, dict):
        problems.append("[github]: must be a table")
        github = {}
    native_protection = github.get("native_protection", True)
    if not isinstance(native_protection, bool):
        problems.append("[github].native_protection: must be a boolean")

    path_classes: dict[str, tuple[str, ...]] = {}
    for name, globs in (data.get("path_classes") or {}).items():
        if not isinstance(globs, list) or any(not isinstance(g, str) or not g for g in globs):
            problems.append(f"[path_classes].{name}: must be a list of glob strings")
            continue
        path_classes[str(name)] = tuple(globs)

    hooks: dict[str, tuple[str, ...]] = {}
    hooks_table = data.get("hooks", {})
    if not isinstance(hooks_table, dict):
        problems.append("[hooks]: must be a table")
    else:
        for name, value in hooks_table.items():
            if name not in HOOK_NAMES:
                problems.append(f"[hooks].{name}: unknown hook; known hooks are {', '.join(HOOK_NAMES)}")
                continue
            commands = _commands(value, f"[hooks].{name}", problems)
            if commands:
                hooks[name] = commands

    if problems:
        raise ConfigError(problems)

    return Profile(
        root=root,
        core_repository=repository,
        core_revision=revision,
        core_path=Path(core_path),
        profiles=tuple(profiles),
        checks=checks,
        env_prefix=env_prefix,
        audit_root=Path(audit_root),
        state_root=state_root,
        state_root_explicit=state_root_explicit,
        contract=contract,
        epoch=epoch,
        sandbox=sandbox,
        toolchain=dict(toolchain),
        aliases=aliases,
        tiers=tiers,
        max_reviews_per_pr=max_reviews,
        max_delta_reviews=max_delta,
        required_sections=tuple(sections),
        native_protection=native_protection,
        path_classes=path_classes,
        hooks=hooks,
        raw=data,
    )


def load_profile(root: Path) -> Profile:
    """Load and validate ``<root>/workflow.toml``."""
    root = Path(root).resolve()
    return validate(load_mapping(root / WORKFLOW_FILE), root)


def hooks_from_base(root: Path, base_ref: str = "origin/main") -> dict[str, tuple[str, ...]]:
    """Return the ``[hooks]`` table as committed at ``base_ref``.

    Privileged hooks come from the approved base, so a candidate cannot change
    its own acceptance command before that change is merged. Raises
    :class:`ConfigError` when the base revision or its file is unavailable;
    callers must treat that as a blocker, never as "no hooks".
    """
    root = Path(root).resolve()
    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{base_ref}:{WORKFLOW_FILE}"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if proc.returncode != 0:
        raise ConfigError([f"{WORKFLOW_FILE} at {base_ref}: unavailable ({proc.stderr.strip()})"])
    try:
        data = tomllib.loads(proc.stdout)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{WORKFLOW_FILE} at {base_ref}: invalid TOML: {exc}"]) from None
    problems: list[str] = []
    hooks: dict[str, tuple[str, ...]] = {}
    for name, value in (data.get("hooks") or {}).items():
        if name in HOOK_NAMES:
            commands = _commands(value, f"[hooks].{name}", problems)
            if commands:
                hooks[name] = commands
    if problems:
        raise ConfigError(problems)
    return hooks


def effective_hooks(profile: Profile, base_ref: str = "origin/main") -> dict[str, tuple[str, ...]]:
    """Merge candidate and base hooks: privileged names always come from the base."""
    base = hooks_from_base(profile.root, base_ref)
    merged = dict(profile.hooks)
    for name in PRIVILEGED_HOOKS:
        if name in base:
            merged[name] = base[name]
        else:
            merged.pop(name, None)
    return merged
