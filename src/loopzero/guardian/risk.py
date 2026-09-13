#!/usr/bin/env python3
"""Deterministic fail-closed candidate policy shared by Guardian parents."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

RISK_PARTS = frozenset(
    {
        "account",
        "accounts",
        "admin",
        "admins",
        "auth",
        "bootstrap",
        "config",
        "cookie",
        "cookies",
        "client",
        "clients",
        "dependencies",
        "dependency",
        "egress",
        "fetcher",
        "fetchers",
        "http",
        "https",
        "identities",
        "identity",
        "integrations",
        "job",
        "jobs",
        "lifespan",
        "middleware",
        "migrations",
        "network",
        "networks",
        "owner",
        "owners",
        "ownership",
        "queue",
        "queues",
        "proxy",
        "proxies",
        "routers",
        "scheduler",
        "schedulers",
        "tasks",
        "tenant",
        "tenants",
        "transport",
        "transports",
        "user",
        "users",
        "webhook",
        "webhooks",
        "worker",
        "workers",
        "alembic_migrations",
    }
)
RISK_STEM_TOKENS = frozenset(
    {
        "access",
        "account",
        "accounts",
        "admin",
        "admins",
        "auth",
        "bootstrap",
        "config",
        "cookie",
        "cookies",
        "client",
        "clients",
        "credential",
        "credentials",
        "dependencies",
        "dependency",
        "dispatch",
        "egress",
        "fetch",
        "fetcher",
        "fetchers",
        "http",
        "https",
        "identities",
        "identity",
        "job",
        "jobs",
        "lifespan",
        "middleware",
        "migration",
        "migrations",
        "network",
        "networks",
        "owner",
        "owners",
        "ownership",
        "permission",
        "permissions",
        "policy",
        "proxy",
        "proxies",
        "queue",
        "queues",
        "router",
        "routers",
        "routes",
        "scheduler",
        "schedulers",
        "secret",
        "secrets",
        "session",
        "sessions",
        "task",
        "tasks",
        "tenant",
        "tenants",
        "token",
        "tokens",
        "transport",
        "transports",
        "user",
        "users",
        "webhook",
        "webhooks",
        "worker",
        "workers",
    }
)
RISK_NAMES = frozenset(
    {
        "config.py",
        "conftest.py",
        "dependencies.py",
        "main.py",
        "middleware.py",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements.txt",
        "routes.py",
        "settings.py",
        "uv.lock",
    }
)
OWNERSHIP_RE = re.compile(
    r"(?:\b|_)(?:account|actor|owner|tenant|user)_ids?\b"
    r"|\b(?:uploader_id|actor_kind|current_user|created_by|CurrentAdmin|CurrentUser)\b"
    r"|\b(?:ownership|access|dedup)_scope\b",
    re.IGNORECASE,
)
SECRET_NAME_TOKENS = frozenset(
    {"SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "KEY", "URL", "URI", "DSN"}
)
OUTBOUND_NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "curl_cffi",
        "http.client",
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "websockets",
    }
)
INTERNAL_OUTBOUND_NETWORK_MODULES = frozenset(
    {
        "app.etl.utils.rss_fetcher",
        "app.integrations",
    }
)
EXECUTION_AUTHORITY_MODULES = frozenset(
    {"ctypes", "importlib", "multiprocessing", "socket", "subprocess"}
)
DYNAMIC_EXECUTION_CALLS = frozenset({"__import__", "compile", "eval", "exec"})
OS_EXECUTION_CALLS = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
    }
)


def reserved_risk_path(path: PurePosixPath) -> bool:
    """Return whether a repository path crosses a Reserved trust boundary."""
    lowered_parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    stem_tokens = set(re.split(r"[^a-z0-9]+", path.stem.lower()))
    return bool(
        lowered_parts.intersection(RISK_PARTS)
        or stem_tokens.intersection(RISK_STEM_TOKENS)
        or name in RISK_NAMES
        or any(
            "dispatch" in part
            or "enqueue" in part
            or "middleware" in part
            or "queue" in part
            or "router" in part
            or "schedul" in part
            or part == "dependencies"
            or part.endswith("_tasks")
            or part.startswith("task_")
            or part.startswith("worker_")
            for part in lowered_parts
        )
    )


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _is_settings_ref(
    node: ast.AST, settings_aliases: set[str], config_aliases: set[str]
) -> bool:
    dotted = _dotted_name(node)
    if dotted in settings_aliases or dotted == "app.config.settings":
        return True
    return any(dotted == f"{alias}.settings" for alias in config_aliases)


def _sensitive_setting(name: str) -> bool:
    return bool(set(name.upper().split("_")).intersection(SECRET_NAME_TOKENS))


def _is_outbound_network_module(module: str) -> bool:
    return any(
        module == boundary or module.startswith(boundary + ".")
        for boundary in (*OUTBOUND_NETWORK_MODULES, *INTERNAL_OUTBOUND_NETWORK_MODULES)
    )


def _is_outbound_network_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id.lower() in {
            "download_url",
            "fetch_url",
            "open_url",
            "send_webhook",
            "urlopen",
        }
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr.lower() == "request":
        return True
    receiver = _dotted_name(node.func.value) or ""
    receiver_tokens = set(re.split(r"[^a-z0-9]+", receiver.lower()))
    return bool(
        receiver_tokens.intersection(
            {"client", "egress", "http", "https", "transport", "web"}
        )
        and node.func.attr.lower()
        in {"delete", "fetch", "get", "head", "patch", "post", "put", "request", "send"}
    )


def _name_bindings(node: ast.AST) -> list[tuple[ast.Name, ast.AST]]:
    """Return ordinary name bindings whose value can carry sensitive authority."""

    def pair(target: ast.AST, value: ast.AST) -> list[tuple[ast.Name, ast.AST]]:
        if isinstance(target, ast.Name):
            return [(target, value)]
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
        ):
            return [
                binding
                for target_item, value_item in zip(target.elts, value.elts, strict=True)
                for binding in pair(target_item, value_item)
            ]
        return []

    if isinstance(node, ast.Assign):
        return [
            binding for target in node.targets for binding in pair(target, node.value)
        ]
    if isinstance(node, ast.AnnAssign):
        return pair(node.target, node.value) if node.value is not None else []
    if isinstance(node, ast.NamedExpr):
        return pair(node.target, node.value)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        positional = [*node.args.posonlyargs, *node.args.args]
        default_start = len(positional) - len(node.args.defaults)
        pairs = list(zip(positional[default_start:], node.args.defaults, strict=True))
        pairs.extend(
            (argument, default)
            for argument, default in zip(
                node.args.kwonlyargs, node.args.kw_defaults, strict=True
            )
            if default is not None
        )
        return [(ast.Name(id=argument.arg), default) for argument, default in pairs]
    return []


def reserved_content_reason(content: str) -> str | None:
    """Return a bounded reason when Python content crosses a trust boundary."""
    if OWNERSHIP_RE.search(content):
        return "candidate contains a user-isolation ownership signal"
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return "candidate Python syntax cannot be risk-classified"

    settings_aliases = {"settings"}
    config_aliases = {"config"}
    unwrap_aliases = {"unwrap_secret"}
    reflective_aliases = {"getattr"}
    os_aliases = {"os"}
    builtins_aliases = {"builtins"}
    dynamic_execution_aliases = set(DYNAMIC_EXECUTION_CALLS)
    environment_aliases: set[str] = set()
    getenv_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in EXECUTION_AUTHORITY_MODULES:
                    return "candidate contains dynamic execution authority"
                if _is_outbound_network_module(alias.name):
                    return "candidate imports outbound network authority"
                bound = alias.asname or alias.name.split(".")[0]
                if alias.name == "app.config":
                    config_aliases.add(bound)
                elif alias.name == "os":
                    os_aliases.add(bound)
                elif alias.name == "builtins":
                    builtins_aliases.add(bound)
        elif isinstance(node, ast.ImportFrom):
            if (
                (
                    node.module
                    and node.module.split(".", 1)[0] in EXECUTION_AUTHORITY_MODULES
                )
                or (
                    node.module == "os"
                    and any(alias.name in OS_EXECUTION_CALLS for alias in node.names)
                )
                or (
                    node.module == "builtins"
                    and any(
                        alias.name in DYNAMIC_EXECUTION_CALLS for alias in node.names
                    )
                )
            ):
                return "candidate contains dynamic execution authority"
            if node.module and _is_outbound_network_module(node.module):
                return "candidate imports outbound network authority"
            for alias in node.names:
                qualified = f"{node.module}.{alias.name}" if node.module else alias.name
                if alias.name == "*" or _is_outbound_network_module(qualified):
                    return "candidate imports outbound network authority"
                bound = alias.asname or alias.name
                if alias.name == "settings" and node.module == "app.config":
                    settings_aliases.add(bound)
                elif alias.name == "config" and node.module == "app":
                    config_aliases.add(bound)
                elif alias.name == "unwrap_secret":
                    unwrap_aliases.add(bound)
                elif node.module == "os" and alias.name == "environ":
                    environment_aliases.add(bound)
                elif node.module == "os" and alias.name == "getenv":
                    getenv_aliases.add(bound)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            for target, value in _name_bindings(node):
                if _is_settings_ref(value, settings_aliases, config_aliases):
                    if target.id not in settings_aliases:
                        settings_aliases.add(target.id)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in unwrap_aliases:
                    if target.id not in unwrap_aliases:
                        unwrap_aliases.add(target.id)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in reflective_aliases:
                    if target.id not in reflective_aliases:
                        reflective_aliases.add(target.id)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in os_aliases:
                    if target.id not in os_aliases:
                        os_aliases.add(target.id)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in builtins_aliases:
                    if target.id not in builtins_aliases:
                        builtins_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Name)
                    and value.id in dynamic_execution_aliases
                ):
                    if target.id not in dynamic_execution_aliases:
                        dynamic_execution_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in builtins_aliases
                    and value.attr in DYNAMIC_EXECUTION_CALLS
                ):
                    if target.id not in dynamic_execution_aliases:
                        dynamic_execution_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in reflective_aliases
                    and len(value.args) >= 2
                    and isinstance(value.args[0], ast.Name)
                    and value.args[0].id in builtins_aliases
                    and isinstance(value.args[1], ast.Constant)
                    and value.args[1].value in DYNAMIC_EXECUTION_CALLS
                ):
                    if target.id not in dynamic_execution_aliases:
                        dynamic_execution_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Subscript)
                    and isinstance(value.value, ast.Attribute)
                    and isinstance(value.value.value, ast.Name)
                    and value.value.value.id in builtins_aliases
                    and value.value.attr == "__dict__"
                    and isinstance(value.slice, ast.Constant)
                    and value.slice.value in DYNAMIC_EXECUTION_CALLS
                ):
                    if target.id not in dynamic_execution_aliases:
                        dynamic_execution_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Attribute)
                    and value.attr == "environ"
                    and isinstance(value.value, ast.Name)
                    and value.value.id in os_aliases
                ) or (isinstance(value, ast.Name) and value.id in environment_aliases):
                    if target.id not in environment_aliases:
                        environment_aliases.add(target.id)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in getenv_aliases:
                    if target.id not in getenv_aliases:
                        getenv_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Attribute)
                    and value.attr == "getenv"
                    and isinstance(value.value, ast.Name)
                    and value.value.id in os_aliases
                ):
                    if target.id not in getenv_aliases:
                        getenv_aliases.add(target.id)
                        changed = True
                elif (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in reflective_aliases
                    and len(value.args) >= 2
                    and isinstance(value.args[0], ast.Name)
                    and value.args[0].id in os_aliases
                    and isinstance(value.args[1], ast.Constant)
                    and value.args[1].value in {"environ", "getenv"}
                ):
                    aliases = (
                        environment_aliases
                        if value.args[1].value == "environ"
                        else getenv_aliases
                    )
                    if target.id not in aliases:
                        aliases.add(target.id)
                        changed = True

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        ):
            if node.attr in OS_EXECUTION_CALLS:
                return "candidate contains dynamic execution authority"
            if node.attr in {"environ", "getenv"}:
                return "candidate contains process environment access"
        if isinstance(node, ast.Name) and node.id in dynamic_execution_aliases:
            return "candidate contains dynamic execution authority"
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in builtins_aliases
            and node.attr in DYNAMIC_EXECUTION_CALLS
        ):
            return "candidate contains dynamic execution authority"
        if isinstance(node, ast.Name) and node.id in environment_aliases:
            return "candidate contains process environment access"
        if isinstance(node, ast.Name) and node.id in getenv_aliases:
            return "candidate contains process environment access"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in reflective_aliases
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in os_aliases
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"environ", "getenv"}
        ):
            return "candidate contains process environment access"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in reflective_aliases
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in os_aliases
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in OS_EXECUTION_CALLS
        ):
            return "candidate contains dynamic execution authority"
        if isinstance(node, ast.Attribute) and _is_settings_ref(
            node.value, settings_aliases, config_aliases
        ):
            if node.attr in {"dict", "json", "model_dump", "model_dump_json"}:
                return "candidate contains reflective settings access"
            if _sensitive_setting(node.attr):
                return "candidate contains direct secret-backed configuration access"
        if isinstance(node, ast.Subscript) and _is_settings_ref(
            node.value, settings_aliases, config_aliases
        ):
            return "candidate contains dynamic settings access"
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name) and node.func.id in DYNAMIC_EXECUTION_CALLS
        ) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in os_aliases
            and node.func.attr in OS_EXECUTION_CALLS
        ):
            return "candidate contains dynamic execution authority"
        if _is_outbound_network_call(node):
            return "candidate contains an outbound network call"
        if any(
            _is_settings_ref(argument, settings_aliases, config_aliases)
            for argument in node.args
        ) or any(
            keyword.value is not None
            and _is_settings_ref(keyword.value, settings_aliases, config_aliases)
            for keyword in node.keywords
        ):
            return "candidate contains reflective settings access"
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in os_aliases
            and node.func.attr == "getenv"
        ) or (isinstance(node.func, ast.Name) and node.func.id in getenv_aliases):
            return "candidate contains process environment access"
        if isinstance(node.func, ast.Attribute) and _is_settings_ref(
            node.func.value, settings_aliases, config_aliases
        ):
            return "candidate contains reflective settings access"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_secret_value"
        ):
            return "candidate directly unwraps a secret value"
        if isinstance(node.func, ast.Name) and node.func.id in unwrap_aliases:
            return "candidate directly unwraps a secret value"
    return None


def classify_candidate(candidate: str, content: str) -> str | None:
    """Return one bounded Reserved reason, or None when eligible."""
    if reserved_risk_path(PurePosixPath(candidate)):
        return "candidate crosses a reserved-risk path"
    return reserved_content_reason(content)
