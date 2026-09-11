"""Authoritative Codex configuration helpers for governed worker runtimes."""

import json
from pathlib import Path

PINNED_CODEX_VERSION = "0.147.0"
MAX_CONFIG_LOCK_BYTES = 512 * 1024

_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "computer_use",
    "enable_mcp_apps",
    "fast_mode",
    "goals",
    "guardian_approval",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "multi_agent_v2",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "workspace_dependencies",
)


def codex_runtime_overrides() -> tuple[str, ...]:
    """Build the closed highest-precedence runtime configuration layer."""
    fixed = (
        'model_provider="openai"',
        'service_tier="default"',
        "mcp_servers={}",
        "hooks={}",
        "plugins={}",
        "marketplaces={}",
        "skills={}",
        "orchestrator.skills.enabled=false",
        "orchestrator.mcp.enabled=false",
        "check_for_update_on_startup=false",
        "include_apps_instructions=false",
        "include_collaboration_mode_instructions=false",
        "features.code_mode_host=true",
    )
    return (*fixed, *(f"features.{name}=false" for name in _DISABLED_FEATURES))


def codex_bootstrap_overrides(export_dir: Path) -> tuple[str, ...]:
    """Build a closed config layer used only to export an effective lock."""
    return (
        f"debug.config_lockfile.export_dir={json.dumps(str(export_dir))}",
        *codex_runtime_overrides(),
    )


def codex_config_lock_override(path: Path) -> str:
    """Return the TOML-safe CLI override that activates authoritative replay."""
    return f"debug.config_lockfile.load_path={json.dumps(str(path))}"


def exported_codex_config_lock(export_dir: Path) -> Path:
    """Resolve one bounded SDK-exported lock or fail closed."""
    locks = list(export_dir.glob("*.config.lock.toml"))
    if len(locks) != 1:
        raise RuntimeError("startup")
    lock = locks[0]
    size = lock.stat().st_size
    if size <= 0 or size > MAX_CONFIG_LOCK_BYTES:
        raise RuntimeError("startup")
    lock.chmod(0o600)
    return lock
