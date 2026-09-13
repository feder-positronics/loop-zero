"""Closed effective-config shape for the pinned Codex 0.154.0 app-server.

Every leaf is exact, including absent (null) capabilities and empty registries.
Unknown keys, even null keys, require review when upgrading the pinned binary.
TUI is disabled (null); history is only the native local persistence default.
The file opener and display defaults are inert in app-server mode. Endpoint,
shell, credential-store, document-discovery and feature defaults are fixed here,
not accepted as arbitrary caller bookkeeping. Request/CLI pins overlay this
baseline before comparison; no values are learned from the untrusted response.
"""

from collections.abc import Mapping
from copy import deepcopy
import tomllib


CODEX_EFFECTIVE_DEFAULTS = {
    "agents": None,
    "allow_login_shell": True,
    "allow_symlinked_codex_home": None,
    "analytics": None,
    "approval_policy": "never",
    "approvals_reviewer": None,
    "apps": None,
    "apps_mcp_product_sku": None,
    "audio": None,
    "auto_review": None,
    "background_terminal_max_timeout": 300000,
    "browser_use": None,
    "chatgpt_base_url": "https://chatgpt.com/backend-api/",
    "check_for_update_on_startup": False,
    "cli_auth_credentials_store": "file",
    "compact_prompt": None,
    "computer_use": None,
    "default_permissions": None,
    "desktop": None,
    "developer_instructions": None,
    "disable_paste_burst": None,
    "experimental_compact_prompt_file": None,
    "experimental_realtime_start_instructions": None,
    "experimental_realtime_webrtc_call_base_url": None,
    "experimental_realtime_ws_backend_prompt": None,
    "experimental_realtime_ws_base_url": None,
    "experimental_realtime_ws_model": None,
    "experimental_realtime_ws_startup_context": None,
    "experimental_thread_store": None,
    "experimental_thread_store_endpoint": None,
    "experimental_use_unified_exec_tool": None,
    "features": {
        "apps": False,
        "auth_elicitation": False,
        "background_paginated_rollout_migration": False,
        "browser_use": False,
        "browser_use_external": False,
        "browser_use_full_cdp_access": False,
        "code_mode": False,
        "code_mode_host": True,
        "computer_use": False,
        "enable_mcp_apps": False,
        "fast_mode": False,
        "goals": False,
        "guardian_approval": False,
        "hooks": False,
        "image_generation": False,
        "in_app_browser": False,
        "mcp_2026_07_28": False,
        "memories": False,
        "mentions_v2": True,
        "multi_agent": False,
        "multi_agent_v2": False,
        "network_proxy": None,
        "plugin_sharing": False,
        "plugins": False,
        "remote_control": False,
        "remote_plugin": False,
        "skill_mcp_dependency_install": False,
        "tool_call_mcp_elicitation": False,
        "tool_suggest": False,
        "windows_sandbox_service": False,
        "workspace_dependencies": False
    },
    "feedback": None,
    "file_opener": "vscode",
    "forced_chatgpt_workspace_id": None,
    "forced_login_method": None,
    "ghost_snapshot": None,
    "goals": None,
    "hide_agent_reasoning": False,
    "history": {
        "max_bytes": None,
        "persistence": "save-all"
    },
    "hooks": {
        "Interrupt": [],
        "PermissionRequest": [],
        "PostCompact": [],
        "PostToolUse": [],
        "PreCompact": [],
        "PreToolUse": [],
        "SessionEnd": [],
        "SessionStart": [],
        "Stop": [],
        "SubagentStart": [],
        "SubagentStop": [],
        "UserPromptSubmit": []
    },
    "include_apps_instructions": False,
    "include_collaboration_mode_instructions": False,
    "include_environment_context": True,
    "include_permissions_instructions": True,
    "instructions": None,
    "js_repl_node_module_dirs": None,
    "js_repl_node_path": None,
    "log_dir": None,
    "marketplaces": {},
    "mcp_oauth_callback_port": None,
    "mcp_oauth_callback_url": None,
    "mcp_oauth_credentials_store": "auto",
    "mcp_optional_startup_grace_ms": None,
    "mcp_servers": {},
    "memories": None,
    "model_auto_compact_token_limit": None,
    "model_auto_compact_token_limit_scope": None,
    "model_catalog_json": None,
    "model_context_window": None,
    "model_instructions_file": None,
    "model_provider": "openai",
    "model_providers": {},
    "model_reasoning_summary": None,
    "model_verbosity": None,
    "notice": None,
    "notify": None,
    "openai_base_url": None,
    "orchestrator": {
        "mcp": {
            "enabled": False
        },
        "skills": {
            "enabled": False
        }
    },
    "oss_provider": None,
    "otel": None,
    "permissions": None,
    "personality": None,
    "plan_mode_reasoning_effort": None,
    "plugins": {},
    "profile": None,
    "profiles": {},
    "project_doc_fallback_filenames": [],
    "project_doc_max_bytes": 32768,
    "project_root_markers": [
        ".git"
    ],
    "projects": None,
    "realtime": None,
    "responses_api_metadata": None,
    "review_model": None,
    "sandbox_workspace_write": None,
    "service_tier": "default",
    "shell_environment_policy": {
        "exclude": None,
        "experimental_use_profile": None,
        "filters": None,
        "ignore_default_excludes": None,
        "include_only": None,
        "inherit": None,
        "set": None
    },
    "show_raw_agent_reasoning": None,
    "skills": {},
    "sqlite_home": None,
    "suppress_unstable_features_warning": None,
    "thread_unload_delay_secs": None,
    "tool_output_token_limit": None,
    "tool_suggest": None,
    "tools": None,
    "tui": None,
    "web_search": None,
    "windows": None
}


def expected_config(overrides: tuple[str, ...]) -> dict[str, object]:
    """Apply only our trusted CLI pins to the reviewed binary defaults."""
    expected = deepcopy(CODEX_EFFECTIVE_DEFAULTS)

    def merge(target, pins):
        for key, value in pins.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = value

    pins = tomllib.loads("\n".join(overrides))
    # 0.154.0 config/read omits the legacy CLI output_token_limit override.
    # Do not invent an effective key or claim to attest that omitted limit.
    # If a future binary exposes it, the unknown effective key fails closed.
    pins.pop("output_token_limit", None)
    merge(expected, pins)
    return expected


def exact_config(actual: object, expected: object) -> bool:
    """Compare recursively without bool/int equivalence or ignored members."""
    if isinstance(expected, Mapping):
        return (
            isinstance(actual, Mapping)
            and actual.keys() == expected.keys()
            and all(exact_config(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(exact_config(a, e) for a, e in zip(actual, expected))
        )
    return type(actual) is type(expected) and actual == expected
