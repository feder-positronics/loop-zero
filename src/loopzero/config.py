"""Load and validate a consumer's ``workflow.toml``.

The file is read by humans, agents and this package. This skeleton validates
``[hooks]`` commands but does not execute them; privileged hooks are read from
the approved base revision through :func:`hooks_from_base`, never from the
candidate worktree. Validation reports every problem at once so a consumer
fixes the file in one pass.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKFLOW_FILE = "workflow.toml"
CONTRACT_ID = "loop-zero-v1"
PROFILES = ("python", "fastapi", "nextjs", "angular")
RUNNERS = ("claude", "codex", "cursor", "acp", "fake")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SANDBOXES = ("bwrap",)
CHECK_GROUPS = ("required", "advisory", "scheduled")
HOOK_NAMES = ("worktree_setup", "acceptance", "db_acceptance", "closeout")
PRIVILEGED_HOOKS = ("acceptance", "db_acceptance", "closeout")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_BASE_REF_RE = re.compile(
    r"^refs/(?:heads/[A-Za-z0-9][A-Za-z0-9._/-]*|"
    r"remotes/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

TOOLCHAIN_KEYS = frozenset(
    {
        "interpreter",
        "shared_artifacts",
        "dotenv",
        "db_url_vars",
        "db_default_url",
        "db_lock",
        "db_targets",
        "dispatcher",
        "host_dispatcher",
        "continuation_runner",
        "formatter_modules",
        "trusted_bin_dir",
        "closeout_snapshot_path",
        "closeout_temp_prefix",
    }
)
ROUTING_KEYS = frozenset(
    {
        "aliases",
        "tiers",
        "budgets",
        "policy_version",
        "telemetry_schema_version",
        "compatible_policy_versions",
        "default_timeout_s",
        "engine_cooldown_s",
        "verifier_models",
    }
)
REVIEW_KEYS = frozenset(
    {
        "max_reviews_per_pr",
        "max_delta_reviews",
        "required_sections",
        "finding_severities",
        "security_patterns",
        "cross_harness_routes",
    }
)
GITHUB_KEYS = frozenset(
    {
        "native_protection",
        "workflow",
        "labels",
        "check_commands",
        "ref_namespace",
        "gh_version_floor",
        "retries",
        "body_required_sections",
    }
)


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
    family: str = ""
    engines: tuple[str, ...] = ()
    backup_model: str | None = None
    allowed_efforts: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
    default_effort: str = "medium"


@dataclass(frozen=True)
class Tier:
    alias: str
    effort: str = "medium"
    read_only: bool = False


@dataclass(frozen=True)
class GithubConfig:
    native_protection: bool = True
    workflow: str = ".github/workflows/ci.yml"
    labels: dict[str, str] = field(default_factory=dict)
    check_commands: dict[str, str] = field(default_factory=dict)
    ref_namespace: str = "refs/heads"
    gh_version_floor: tuple[int, int, int] = (2, 40, 0)
    retries: int = 2
    body_required_sections: tuple[str, ...] = ()


DEFAULT_ROUTING_ALIASES: dict[str, dict[str, Any]] = {
    "fable": {
        "runner": "claude", "model": "claude-fable-5-1", "family": "claude",
        "engines": ["claude", "cursor"], "backup_model": "auto",
        "allowed_efforts": ["medium", "high"], "default_effort": "medium",
    },
    "fable-cursor": {
        "runner": "cursor", "model": "claude-fable-5-medium", "family": "cursor",
        "engines": ["cursor"], "allowed_efforts": ["medium"],
        "default_effort": "medium",
    },
    "grok-cursor": {
        "runner": "cursor", "model": "cursor-grok-4.6-high", "family": "cursor",
        "engines": ["cursor"], "allowed_efforts": ["high"],
        "default_effort": "high",
    },
    "opus": {
        "runner": "claude", "model": "claude-opus-5", "family": "claude",
        "engines": ["claude", "cursor"], "backup_model": "auto",
        "allowed_efforts": ["medium", "high"], "default_effort": "high",
    },
    "sol": {
        "runner": "codex", "model": "gpt-5.6-sol", "family": "gpt",
        "engines": ["codex", "cursor"], "backup_model": "auto",
        "allowed_efforts": ["medium", "high"], "default_effort": "medium",
        "write": True,
    },
    "terra": {
        "runner": "codex", "model": "gpt-5.6-terra", "family": "gpt",
        "engines": ["codex", "cursor"], "backup_model": "auto",
        "allowed_efforts": ["medium", "high"], "default_effort": "high",
        "write": True,
    },
    "luna": {
        "runner": "codex", "model": "gpt-5.6-luna", "family": "gpt",
        "engines": ["codex", "cursor"], "backup_model": "auto",
        "allowed_efforts": ["medium", "high", "xhigh"],
        "default_effort": "high", "write": True,
    },
    "auto": {
        "runner": "cursor", "model": "auto", "family": "cursor",
        "engines": ["cursor"], "allowed_efforts": ["medium", "high", "xhigh"],
        "default_effort": "medium", "write": True,
    },
}

DEFAULT_ROUTING_TIERS: dict[str, dict[str, Any]] = {
    "S": {"alias": "sol", "effort": "high"},
    "S-deep": {"alias": "sol", "effort": "high"},
    "S-cross-review": {"alias": "opus", "effort": "high", "read_only": True},
    "A-architecture-review": {"alias": "opus", "effort": "high", "read_only": True},
    "S-exceptional": {"alias": "fable", "effort": "medium"},
    "S-exceptional-cursor": {"alias": "fable-cursor", "effort": "medium", "read_only": True},
    "A": {"alias": "sol", "effort": "medium"},
    "A-light": {"alias": "sol", "effort": "medium"},
    "B": {"alias": "luna", "effort": "xhigh"},
    "B-terra": {"alias": "terra", "effort": "high"},
    "B-moderate": {"alias": "terra", "effort": "medium"},
    "B-claude": {"alias": "opus", "effort": "high"},
    "C": {"alias": "luna", "effort": "high"},
    "C-hard": {"alias": "luna", "effort": "high"},
    "C-mechanical": {"alias": "luna", "effort": "medium"},
    "C-light": {"alias": "opus", "effort": "medium"},
    "C-auto": {"alias": "auto", "effort": "medium"},
    "C-ui": {"alias": "auto", "effort": "medium"},
}

DEFAULT_VERIFIER_MODELS = {
    "astra": "gpt-6-astra",
    "gpt-6-astra": "gpt-6-astra",
    "grok-cursor": "cursor-grok-4.6-high",
}


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
    verifier_models: dict[str, str] = field(default_factory=dict)
    routing_budgets: dict[str, float] = field(default_factory=dict)
    routing_policy_version: str = "2026-08-17-v11"
    telemetry_schema_version: str = "dispatch-telemetry-v9"
    compatible_policy_versions: tuple[str, ...] = ()
    default_timeout_s: int = 900
    engine_cooldown_s: int = 600
    max_reviews_per_pr: int = 1
    max_delta_reviews: int = 1
    required_sections: tuple[str, ...] = ("code", "security")
    finding_severities: tuple[str, ...] = ("critical", "important", "suggestion")
    security_patterns: tuple[str, ...] = ()
    cross_harness_routes: dict[str, str] = field(
        default_factory=lambda: {"codex": "opus", "claude": "sol"}
    )
    github: GithubConfig = field(default_factory=GithubConfig)
    path_classes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    path_class_parents: dict[str, str] = field(
        default_factory=lambda: {
            "backend-risk": "backend",
            "frontend-e2e": "frontend",
            "blueprints": "docs",
            "governance": "docs",
        }
    )
    hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def native_protection(self) -> bool:
        """Compatibility spelling retained for the skeleton's callers."""
        return self.github.native_protection

    @property
    def snapshot_dir(self) -> Path:
        return self.root / self.core_path

    def snapshot_version(self) -> str | None:
        version_file = self.snapshot_dir / "VERSION"
        try:
            if not stat.S_ISREG(os.lstat(version_file).st_mode):
                return None
            return version_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise ConfigError([f"{version_file}: cannot read snapshot version: {exc}"]) from None


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError([f"{path}: missing"]) from None
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise ConfigError([f"{path}: invalid TOML: {exc}"]) from None
    except OSError as exc:
        raise ConfigError([f"{path}: cannot read: {exc}"]) from None


def _commands(value: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else _fail(problems, f"{where}: empty command")
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return tuple(value)
    return _fail(problems, f"{where}: must be a string or a list of nonempty strings")


def _fail(problems: list[str], message: str) -> tuple[str, ...]:
    problems.append(message)
    return ()


def _unknown_fields(
    table: dict[str, Any], allowed: frozenset[str], where: str, problems: list[str]
) -> None:
    for name in sorted(table.keys() - allowed):
        problems.append(f"{where}.{name}: unknown field")


def _string(
    table: dict[str, Any], name: str, default: str, where: str, problems: list[str]
) -> str:
    value = table.get(name, default)
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}.{name}: must be a nonempty string")
        return default
    return value


def _strings(
    table: dict[str, Any], name: str, default: tuple[str, ...], where: str,
    problems: list[str], *, paths: bool = False,
) -> tuple[str, ...]:
    value = table.get(name, list(default))
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        problems.append(f"{where}.{name}: must be an array of nonempty strings")
        return default
    if paths and any(Path(item).is_absolute() or ".." in Path(item).parts for item in value):
        problems.append(f"{where}.{name}: paths must be repository-relative without traversal")
        return default
    return tuple(value)


def _relative_path(
    table: dict[str, Any], name: str, default: str, where: str, problems: list[str]
) -> str:
    value = _string(table, name, default, where, problems)
    if Path(value).is_absolute() or ".." in Path(value).parts:
        problems.append(f"{where}.{name}: must be repository-relative without traversal")
        return default
    return value


def _version_tuple(value: str, where: str, problems: list[str]) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        problems.append(f"{where}: must be a dotted three-integer version")
        return (2, 40, 0)
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _hooks(data: dict[str, Any], where: str, problems: list[str]) -> dict[str, tuple[str, ...]]:
    """Validate hooks identically for candidate and committed base policy."""
    hooks: dict[str, tuple[str, ...]] = {}
    hooks_table = data.get("hooks", {})
    if not isinstance(hooks_table, dict):
        problems.append(f"{where}: must be a table")
        return hooks
    for name, value in hooks_table.items():
        item_where = f"{where}.{name}"
        if name not in HOOK_NAMES:
            problems.append(
                f"{item_where}: unknown hook; known hooks are {', '.join(HOOK_NAMES)}"
            )
            continue
        commands = _commands(value, item_where, problems)
        if commands:
            hooks[name] = commands
    return hooks


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
    if (
        not isinstance(core_path, str)
        or not core_path.strip()
        or Path(core_path).is_absolute()
        or not Path(core_path).parts
        or ".." in Path(core_path).parts
    ):
        problems.append("[core].path: must be a nonempty repository-relative path without traversal")
    else:
        current = root.resolve()
        for part in Path(core_path).parts:
            current = current / part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    problems.append(f"[core].path: symlink component {current} is forbidden")
                    break
            except FileNotFoundError:
                break
            except OSError as exc:
                problems.append(f"[core].path: cannot inspect {current}: {exc}")
                break

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
    _unknown_fields(toolchain, TOOLCHAIN_KEYS, "[toolchain]", problems)
    for key in ("interpreter", "dotenv", "dispatcher", "host_dispatcher", "continuation_runner",
                "closeout_snapshot_path"):
        if key in toolchain:
            _relative_path(toolchain, key, "unused", "[toolchain]", problems)
    if "db_default_url" in toolchain:
        _string(toolchain, "db_default_url", "unused", "[toolchain]", problems)
    for key in ("shared_artifacts", "formatter_modules"):
        if key in toolchain:
            _strings(toolchain, key, (), "[toolchain]", problems, paths=True)
    for key in ("db_url_vars", "db_targets"):
        if key in toolchain:
            _strings(toolchain, key, (), "[toolchain]", problems)
    if "db_url_vars" in toolchain:
        for name in toolchain.get("db_url_vars", []):
            if not _PREFIX_RE.fullmatch(name):
                problems.append(f"[toolchain].db_url_vars: {name!r} is not an environment name")
    if "db_lock" in toolchain:
        lock = toolchain["db_lock"]
        if not isinstance(lock, str) or not lock or not Path(lock).is_absolute():
            problems.append("[toolchain].db_lock: must be a nonempty absolute path")
    if "trusted_bin_dir" in toolchain:
        trusted_bin = toolchain["trusted_bin_dir"]
        if not isinstance(trusted_bin, str) or not trusted_bin or not Path(trusted_bin).is_absolute():
            problems.append("[toolchain].trusted_bin_dir: must be a nonempty absolute path")
    if "closeout_temp_prefix" in toolchain:
        prefix = toolchain["closeout_temp_prefix"]
        if not isinstance(prefix, str) or not prefix or "/" in prefix:
            problems.append("[toolchain].closeout_temp_prefix: must be a nonempty filename prefix")

    routing = data.get("routing", {})
    aliases: dict[str, Alias] = {}
    tiers: dict[str, Tier] = {}
    if not isinstance(routing, dict):
        problems.append("[routing]: must be a table")
        routing = {}
    else:
        _unknown_fields(routing, ROUTING_KEYS, "[routing]", problems)
        using_default_aliases = "aliases" not in routing
        aliases_table = routing.get("aliases", DEFAULT_ROUTING_ALIASES)
        if not isinstance(aliases_table, dict):
            problems.append("[routing.aliases]: must be a table")
            aliases_table = {}
        for name, spec in aliases_table.items():
            where = f"[routing.aliases].{name}"
            if not _ALIAS_RE.match(str(name)):
                problems.append(f"{where}: alias names match [a-z][a-z0-9-]*")
            if not isinstance(spec, dict):
                problems.append(f"{where}: must be a table")
                continue
            unknown = spec.keys() - {
                "runner", "model", "write", "agent", "family", "engines",
                "backup_model", "allowed_efforts", "default_effort",
            }
            for field_name in sorted(unknown):
                problems.append(f"{where}.{field_name}: unknown field")
            runner = spec.get("runner")
            model = spec.get("model")
            write = spec.get("write", False)
            agent = spec.get("agent")
            family = spec.get("family", runner)
            engines = spec.get("engines", [runner] if isinstance(runner, str) else [])
            backup_model = spec.get("backup_model")
            allowed_efforts = spec.get("allowed_efforts", ["medium", "high", "xhigh"])
            default_effort = spec.get("default_effort", "medium")
            valid = True
            if runner not in RUNNERS:
                problems.append(f"{where}: runner must be one of {', '.join(RUNNERS)}")
                valid = False
            if not isinstance(model, str) or not model:
                problems.append(f"{where}: model must be a nonempty string")
                valid = False
            if not isinstance(write, bool):
                problems.append(f"{where}.write: must be a boolean")
                valid = False
            if agent is not None and not isinstance(agent, str):
                problems.append(f"{where}.agent: must be a string or absent")
                valid = False
            if not isinstance(family, str) or not family:
                problems.append(f"{where}.family: must be a nonempty string")
                valid = False
            if (
                not isinstance(engines, list)
                or not engines
                or any(engine not in RUNNERS for engine in engines)
                or len(set(engines)) != len(engines)
                or (isinstance(runner, str) and engines[0] != runner)
            ):
                problems.append(
                    f"{where}.engines: must be a unique nonempty runner list beginning with runner"
                )
                valid = False
            if backup_model is not None and (
                not isinstance(backup_model, str) or not backup_model
            ):
                problems.append(f"{where}.backup_model: must be a nonempty string or absent")
                valid = False
            if (
                not isinstance(allowed_efforts, list)
                or not allowed_efforts
                or any(item not in EFFORTS for item in allowed_efforts)
                or len(set(allowed_efforts)) != len(allowed_efforts)
            ):
                problems.append(
                    f"{where}.allowed_efforts: must be a unique nonempty effort list"
                )
                valid = False
            if default_effort not in EFFORTS or (
                isinstance(allowed_efforts, list) and default_effort not in allowed_efforts
            ):
                problems.append(f"{where}.default_effort: must be one of allowed_efforts")
                valid = False
            if valid:
                aliases[name] = Alias(
                    runner=runner,
                    model=model,
                    write=write,
                    agent=agent,
                    family=family,
                    engines=tuple(engines),
                    backup_model=backup_model,
                    allowed_efforts=tuple(allowed_efforts),
                    default_effort=default_effort,
                )

        tiers_table = routing.get(
            "tiers", DEFAULT_ROUTING_TIERS if using_default_aliases else {}
        )
        if not isinstance(tiers_table, dict):
            problems.append("[routing.tiers]: must be a table")
            tiers_table = {}
        for name, spec in tiers_table.items():
            where = f"[routing.tiers].{name}"
            if not isinstance(spec, dict):
                problems.append(f"{where}: must be a table")
                continue
            unknown = spec.keys() - {"alias", "effort", "read_only"}
            for field_name in sorted(unknown):
                problems.append(f"{where}.{field_name}: unknown field")
            alias = spec.get("alias")
            effort = spec.get("effort", "medium")
            read_only = spec.get("read_only", False)
            valid = True
            if not isinstance(alias, str) or alias not in aliases:
                problems.append(f"{where}: alias must name an entry in [routing.aliases]")
                valid = False
            if not isinstance(effort, str) or effort not in EFFORTS:
                problems.append(f"{where}.effort: must be one of {', '.join(EFFORTS)}")
                valid = False
            if not isinstance(read_only, bool):
                problems.append(f"{where}.read_only: must be a boolean")
                valid = False
            if valid:
                tiers[name] = Tier(alias=alias, effort=effort, read_only=read_only)

    budgets_table = routing.get("budgets", {"medium": 5.0, "high": 10.0})
    routing_budgets: dict[str, float] = {}
    if not isinstance(budgets_table, dict):
        problems.append("[routing.budgets]: must be a table")
    else:
        for name, value in budgets_table.items():
            if not isinstance(name, str) or not _ALIAS_RE.fullmatch(name):
                problems.append(f"[routing.budgets].{name}: budget names match [a-z][a-z0-9-]*")
            elif type(value) not in (int, float) or value < 0:
                problems.append(f"[routing.budgets].{name}: must be a non-negative number")
            else:
                routing_budgets[name] = float(value)
    policy_version = _string(routing, "policy_version", "2026-08-17-v11", "[routing]", problems)
    telemetry_version = _string(
        routing, "telemetry_schema_version", "dispatch-telemetry-v9", "[routing]", problems
    )
    for label, value in (("policy_version", policy_version), ("telemetry_schema_version", telemetry_version)):
        if not _VERSION_RE.fullmatch(value):
            problems.append(f"[routing].{label}: contains unsupported characters")
    compatible_versions = _strings(
        routing,
        "compatible_policy_versions",
        ("2026-07-24-v9", "2026-08-06-v10", policy_version),
        "[routing]",
        problems,
    )
    if any(not _VERSION_RE.fullmatch(value) for value in compatible_versions):
        problems.append("[routing].compatible_policy_versions: contains an invalid version")
    default_timeout_s = routing.get("default_timeout_s", 900)
    engine_cooldown_s = routing.get("engine_cooldown_s", 600)
    for label, value in (("default_timeout_s", default_timeout_s), ("engine_cooldown_s", engine_cooldown_s)):
        if type(value) is not int or value < 1:
            problems.append(f"[routing].{label}: must be a positive integer")

    verifier_table = routing.get("verifier_models", DEFAULT_VERIFIER_MODELS)
    verifier_models: dict[str, str] = {}
    if not isinstance(verifier_table, dict):
        problems.append("[routing.verifier_models]: must be a table")
    else:
        for alias, model in verifier_table.items():
            if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
                problems.append(
                    f"[routing.verifier_models].{alias}: alias names match [a-z][a-z0-9-]*"
                )
            elif not isinstance(model, str) or not model:
                problems.append(f"[routing.verifier_models].{alias}: must be a nonempty string")
            else:
                verifier_models[alias] = model

    review = data.get("review", {})
    if not isinstance(review, dict):
        problems.append("[review]: must be a table")
        review = {}
    _unknown_fields(review, REVIEW_KEYS, "[review]", problems)
    max_reviews = review.get("max_reviews_per_pr", 1)
    max_delta = review.get("max_delta_reviews", 1)
    for label, value in (("max_reviews_per_pr", max_reviews), ("max_delta_reviews", max_delta)):
        if type(value) is not int or value < 0:
            problems.append(f"[review].{label}: must be a non-negative integer")
    sections = review.get("required_sections", ["code", "security"])
    if not isinstance(sections, list) or any(not isinstance(s, str) for s in sections):
        problems.append("[review].required_sections: must be a list of strings")
        sections = []
    elif any(not s.strip() or not _LABEL_KEY_RE.fullmatch(s) for s in sections):
        problems.append("[review].required_sections: section names must be nonempty lowercase identifiers")
    elif len(sections) != len(set(sections)):
        problems.append("[review].required_sections: entries must be unique")
    finding_severities = _strings(
        review, "finding_severities", ("critical", "important", "suggestion"), "[review]", problems
    )
    if len(set(finding_severities)) != len(finding_severities):
        problems.append("[review].finding_severities: entries must be unique")
    if any(value not in {"critical", "important", "suggestion"} for value in finding_severities):
        problems.append(
            "[review].finding_severities: values must be critical, important, or suggestion"
        )
    security_patterns = _strings(review, "security_patterns", (), "[review]", problems)
    cross_harness_table = review.get(
        "cross_harness_routes",
        {"codex": "opus", "claude": "sol"} if using_default_aliases else {},
    )
    cross_harness_routes: dict[str, str] = {}
    if not isinstance(cross_harness_table, dict):
        problems.append("[review].cross_harness_routes: must be a table")
    else:
        for harness_name, alias_name in cross_harness_table.items():
            if harness_name not in {"claude", "codex"}:
                problems.append(
                    f"[review].cross_harness_routes.{harness_name}: unknown harness"
                )
            elif not isinstance(alias_name, str) or alias_name not in aliases:
                problems.append(
                    f"[review].cross_harness_routes.{harness_name}: must name a configured alias"
                )
            else:
                cross_harness_routes[harness_name] = alias_name

    github = data.get("github", {})
    if not isinstance(github, dict):
        problems.append("[github]: must be a table")
        github = {}
    _unknown_fields(github, GITHUB_KEYS, "[github]", problems)
    native_protection = github.get("native_protection", True)
    if not isinstance(native_protection, bool):
        problems.append("[github].native_protection: must be a boolean")
    github_workflow = _relative_path(
        github, "workflow", ".github/workflows/ci.yml", "[github]", problems
    )
    labels_table = github.get("labels", {})
    labels: dict[str, str] = {}
    if not isinstance(labels_table, dict):
        problems.append("[github].labels: must be a table")
    else:
        for name, value in labels_table.items():
            if not _LABEL_KEY_RE.fullmatch(str(name)):
                problems.append(f"[github].labels.{name}: label keys must be lowercase identifiers")
            elif not isinstance(value, str) or not value.strip():
                problems.append(f"[github].labels.{name}: must be a nonempty string")
            else:
                labels[str(name)] = value
    commands_table = github.get("check_commands", {})
    check_commands: dict[str, str] = {}
    if not isinstance(commands_table, dict):
        problems.append("[github.check_commands]: must be a table")
    else:
        for name, value in commands_table.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, str) or not value.strip():
                problems.append("[github.check_commands]: names and commands must be nonempty strings")
            else:
                check_commands[name] = value
    ref_namespace = _string(github, "ref_namespace", "refs/heads", "[github]", problems)
    if not ref_namespace.startswith("refs/") or ".." in ref_namespace or ref_namespace.endswith("/"):
        problems.append("[github].ref_namespace: must be a normalized refs/ namespace")
    gh_floor = github.get("gh_version_floor", "2.40.0")
    if not isinstance(gh_floor, str):
        problems.append("[github].gh_version_floor: must be a dotted version string")
        gh_floor = "2.40.0"
    gh_version_floor = _version_tuple(gh_floor, "[github].gh_version_floor", problems)
    github_retries = github.get("retries", 2)
    if type(github_retries) is not int or not 0 <= github_retries <= 10:
        problems.append("[github].retries: must be an integer from 0 through 10")
    body_sections = _strings(github, "body_required_sections", (), "[github]", problems)
    if len(body_sections) != len(set(body_sections)):
        problems.append("[github].body_required_sections: entries must be unique")

    path_classes: dict[str, tuple[str, ...]] = {}
    path_classes_table = data.get("path_classes", {})
    if not isinstance(path_classes_table, dict):
        problems.append("[path_classes]: must be a table")
        path_classes_table = {}
    for name, globs in path_classes_table.items():
        if not _LABEL_KEY_RE.fullmatch(str(name)):
            problems.append(f"[path_classes].{name}: class names must be lowercase identifiers")
            continue
        if not isinstance(globs, list) or any(not isinstance(g, str) or not g for g in globs):
            problems.append(f"[path_classes].{name}: must be a list of glob strings")
            continue
        if len(globs) != len(set(globs)) or any(
            "\0" in glob or Path(glob.removeprefix("!")).is_absolute()
            for glob in globs
        ):
            problems.append(
                f"[path_classes].{name}: globs must be unique relative patterns"
            )
            continue
        path_classes[str(name)] = tuple(globs)

    parent_table = data.get(
        "path_class_parents",
        {
            "backend-risk": "backend",
            "frontend-e2e": "frontend",
            "blueprints": "docs",
            "governance": "docs",
        }
        if not path_classes
        else {},
    )
    path_class_parents: dict[str, str] = {}
    if not isinstance(parent_table, dict):
        problems.append("[path_class_parents]: must be a table")
    else:
        for child, parent in parent_table.items():
            if not isinstance(child, str) or not _ALIAS_RE.fullmatch(child):
                problems.append(f"[path_class_parents].{child}: invalid child class")
            elif not isinstance(parent, str) or not _ALIAS_RE.fullmatch(parent):
                problems.append(f"[path_class_parents].{child}: invalid parent class")
            elif path_classes and (child not in path_classes or parent not in path_classes):
                problems.append(
                    f"[path_class_parents].{child}: child and parent must name path classes"
                )
            else:
                path_class_parents[child] = parent

    hooks = _hooks(data, "[hooks]", problems)

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
        toolchain={
            "interpreter": "fastapi_backend/.venv/bin/python",
            "shared_artifacts": [
                "fastapi_backend/.venv", "nextjs-frontend/node_modules"
            ],
            "dotenv": "fastapi_backend/.env",
            "db_url_vars": ["TEST_DATABASE_URL"],
            "db_default_url": "postgresql://localhost:5433/test_db",
            "db_lock": "/tmp/intelflo-testdb.lock",
            "db_targets": [
                "test", "test-be", "test-be-integration", "test-be-slow",
                "test-be-critical-integration", "test-be-coverage",
                "test-be-warnings", "test-be-profile", "test-be-precommit",
                "test-be-precommit-critical", "test-quota-gate-coverage",
            ],
            "dispatcher": "scripts/util/agent_dispatch.py",
            "host_dispatcher": "scripts/util/agent_dispatch_host.py",
            "continuation_runner": "scripts/util/delivery_continuation_worker.py",
            "formatter_modules": ["nextjs-frontend/node_modules"],
            "trusted_bin_dir": "/usr/bin",
            "closeout_snapshot_path": "scripts/util",
            "closeout_temp_prefix": "intelflo-closeout-",
            **toolchain,
        },
        aliases=aliases,
        tiers=tiers,
        verifier_models=verifier_models,
        routing_budgets=routing_budgets,
        routing_policy_version=policy_version,
        telemetry_schema_version=telemetry_version,
        compatible_policy_versions=compatible_versions,
        default_timeout_s=default_timeout_s,
        engine_cooldown_s=engine_cooldown_s,
        max_reviews_per_pr=max_reviews,
        max_delta_reviews=max_delta,
        required_sections=tuple(sections),
        finding_severities=finding_severities,
        security_patterns=security_patterns,
        cross_harness_routes=cross_harness_routes,
        github=GithubConfig(
            native_protection=native_protection,
            workflow=github_workflow,
            labels=labels,
            check_commands=check_commands,
            ref_namespace=ref_namespace,
            gh_version_floor=gh_version_floor,
            retries=github_retries,
            body_required_sections=body_sections,
        ),
        path_classes=path_classes,
        path_class_parents=path_class_parents,
        hooks=hooks,
        raw=data,
    )


def load_profile(root: Path) -> Profile:
    """Load and validate ``<root>/workflow.toml``."""
    root = Path(root).resolve()
    return validate(load_mapping(root / WORKFLOW_FILE), root)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, env=environment
    )


def resolve_base(root: Path, *, base: str | None = None, base_ref: str | None = None) -> str:
    """Resolve and verify one explicit trusted base selection to a commit SHA."""
    if (base is None) == (base_ref is None):
        raise ConfigError(["select exactly one of --base or --base-ref"])
    if base is not None and not _HEX_SHA_RE.fullmatch(base):
        raise ConfigError(["--base must be a full 40-character hexadecimal commit SHA"])
    if base_ref is not None and (
        not _BASE_REF_RE.fullmatch(base_ref)
        or ".." in base_ref
        or "//" in base_ref
        or base_ref.endswith(("/", "."))
        or any(part.startswith(".") or part.endswith(".lock") for part in base_ref.split("/"))
    ):
        raise ConfigError(
            [
                "--base-ref must be a full named ref under refs/heads/<branch> "
                "or refs/remotes/<remote>/<branch>"
            ]
        )
    requested = base if base is not None else base_ref
    root = root.resolve()
    if base_ref is not None:
        ref_proc = _git(root, "show-ref", "--verify", base_ref)
        if ref_proc.returncode != 0:
            detail = ref_proc.stderr.strip() or "named ref is unavailable"
            raise ConfigError([f"base {base_ref!r}: unavailable ({detail})"])
    proc = _git(root, "rev-parse", "--verify", "--end-of-options", f"{requested}^{{commit}}")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not _SHA_RE.fullmatch(sha):
        detail = proc.stderr.strip() or "does not identify an available commit"
        raise ConfigError([f"base {requested!r}: unavailable ({detail})"])
    if base is not None and sha != base.lower():
        raise ConfigError([f"--base {base}: did not resolve to that exact commit"])
    return sha


def hooks_from_base(root: Path, base_sha: str) -> dict[str, tuple[str, ...]]:
    """Return the validated ``[hooks]`` table committed at ``base_sha``.

    Privileged hooks come from the approved base, so a candidate cannot change
    its own acceptance command before that change is merged. Raises
    :class:`ConfigError` when the base revision or its file is unavailable;
    callers must treat that as a blocker, never as "no hooks".
    """
    root = Path(root).resolve()
    sha = resolve_base(root, base=base_sha)
    tree = _git(root, "ls-tree", sha, "--", WORKFLOW_FILE)
    if tree.returncode != 0:
        raise ConfigError([f"{WORKFLOW_FILE} at {sha}: unavailable ({tree.stderr.strip()})"])
    fields = tree.stdout.rstrip("\n").split(None, 3)
    if len(fields) != 4 or fields[0] not in ("100644", "100755") or fields[1] != "blob":
        raise ConfigError([f"{WORKFLOW_FILE} at {sha}: must be a regular blob"])
    try:
        proc = _git(root, "cat-file", "-p", f"{sha}:{WORKFLOW_FILE}")
    except UnicodeError as exc:
        raise ConfigError([f"{WORKFLOW_FILE} at {sha}: is not UTF-8 ({exc})"]) from None
    if proc.returncode != 0:
        raise ConfigError([f"{WORKFLOW_FILE} at {sha}: unavailable ({proc.stderr.strip()})"])
    try:
        data = tomllib.loads(proc.stdout)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{WORKFLOW_FILE} at {sha}: invalid TOML: {exc}"]) from None
    problems: list[str] = []
    hooks = _hooks(data, f"[hooks] at {sha}", problems)
    if problems:
        raise ConfigError(problems)
    return hooks


def effective_hooks(profile: Profile, base_sha: str) -> dict[str, tuple[str, ...]]:
    """Merge candidate and base hooks: privileged names always come from the base."""
    base = hooks_from_base(profile.root, base_sha)
    merged = dict(profile.hooks)
    for name in PRIVILEGED_HOOKS:
        if name in base:
            merged[name] = base[name]
        else:
            merged.pop(name, None)
    return merged
