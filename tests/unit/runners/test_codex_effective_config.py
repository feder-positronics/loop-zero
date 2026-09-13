"""Closed configuration attestation, including offline native merge regressions."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.runners import bridge


FIXTURE = Path(__file__).with_name("fixtures") / "codex_effective_config_0_154_0.json"


def request(tmp_path):
    return dict(cwd=str(tmp_path), requested_model="gpt-5.6-sol", effort="high", read_only=True)


# Reviewer rows plus nested/unknown-key regressions. Even inert customization
# requires review: only the recorded app-server display/history defaults pass.
MUTATIONS = [
    ("notify", ["/bin/sh", "-c", "false"]),
    ("shell_environment_policy.set", {"BASH_ENV": "/tmp/hook.sh"}),
    ("shell_environment_policy.inherit", "all"),
    ("shell_environment_policy.experimental_use_profile", True),
    ("shell_environment_policy.unknown", None),
    ("openai_base_url", "http://127.0.0.1:9/v1"),
    ("chatgpt_base_url", "http://127.0.0.1:9"),
    ("model_providers.other", {"base_url": "http://127.0.0.1:9"}),
    ("model_providers.openai", {"base_url": "http://127.0.0.1:9"}),
    ("mcp_servers.hostile", {"command": "/bin/false"}),
    ("hooks.SessionStart", [{"hooks": [{"command": "/bin/false"}]}]),
    ("hooks.SessionStart", {"nested": []}),
    ("hooks.Unknown", []),
    ("features.hooks", True),
    ("features.hooks", {"SessionStart": {"command": "/bin/false"}}),
    ("features.mcp_servers", {"hostile": {"command": "/bin/false"}}),
    ("features.unknown", False),
    ("features.network_proxy", True),
    ("features.code_mode_host", 1),
    ("sandbox_workspace_write", {"network_access": True}),
    ("sandbox_workspace_write", {"unknown": None}),
    ("MCP_Servers", {"hostile": {"command": "/bin/false"}}),
    ("mcpServers", {"hostile": {"command": "/bin/false"}}),
    ("mcp", {"servers": {"hostile": {"command": "/bin/false"}}}),
    ("profile", "hostile"),
    ("profiles.hostile", {"model": "other"}),
    ("tui", {"notifications": True}),
    ("history.unknown", "injected"),
    ("history.persistence", "none"),
    ("orchestrator.skills.path", "/tmp/skills"),
    ("skills.config", [{"path": "/tmp/skill", "enabled": True}]),
    ("plugins.other", {"enabled": True}),
    ("unknown", None),
    ("output_token_limit", 32768),
    ("tools", {"unknown": {"command": "/bin/false"}}),
]


@pytest.mark.parametrize("key,value", MUTATIONS)
def test_effective_config_allowlist_rejects_every_mutation(tmp_path, key, value):
    payload = json.loads(FIXTURE.read_text())
    target = payload["config"]
    *parents, leaf = key.split(".")
    for parent in parents:
        target = target[parent]
    target[leaf] = value

    class Client:
        def request(self, method, params, *, response_model):
            assert method == "config/read"
            return response_model.model_validate(payload)

    with pytest.raises(RuntimeError, match="unavailable"):
        bridge._enforce_codex_effective_config(SimpleNamespace(_client=Client()), request(tmp_path))


def test_effective_config_accepts_only_recorded_inert_defaults(tmp_path):
    config = json.loads(FIXTURE.read_text())["config"]
    assert config["tui"] is None  # No TUI runs in app-server mode.
    assert config["history"] == {"persistence": "save-all", "max_bytes": None}
    assert bridge._codex_effective_config_is_closed(config, request(tmp_path))
    for key in config:
        missing = dict(config)
        del missing[key]
        assert not bridge._codex_effective_config_is_closed(missing, request(tmp_path)), key


@pytest.mark.parametrize("read_only,outer,effort,budget", [
    (True, False, "high", None),
    (True, False, "low", 32768),
    (False, False, "high", 32768),
    (True, True, "max", 32768),
])
@pytest.mark.parametrize("toml,accepted", [
    ("", True),
    ('notify=["/bin/false"]', False),
    ('[shell_environment_policy.set]\nBASH_ENV="/tmp/hook.sh"', False),
    ('openai_base_url="http://127.0.0.1:9/v1"', False),
    ('chatgpt_base_url="http://127.0.0.1:9"', False),
    ('[model_providers.other]\nname="other"\nbase_url="http://127.0.0.1:9"\nwire_api="responses"', False),
    ('[mcp_servers.hostile]\ncommand="/bin/false"', False),
    ('[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype="command"\ncommand="/bin/false"', False),
    ('[features]\nnetwork_proxy=true', False),
    ('[sandbox_workspace_write]\nnetwork_access=true', False),
    ('[tui]\nnotifications=true', False),
    ('[history]\npersistence="save-all"', True),
    # Native merging neutralizes these inputs; no hostile setting survives.
    ('[features]\nhooks=true', True),
    ('[features.hooks.SessionStart]\ncommand="/bin/false"', True),
    ('[MCP_Servers.hostile]\ncommand="/bin/false"', True),
    ('[mcpServers.hostile]\ncommand="/bin/false"', True),
    ('[mcp.servers.hostile]\ncommand="/bin/false"', True),
])
def test_pinned_binary_config_read_offline(
    tmp_path, monkeypatch, toml, accepted, read_only, outer, effort, budget
):
    from dataclasses import replace
    from codex_cli_bin import bundled_codex_path
    from openai_codex.client import CodexClient, CodexConfig
    from loopzero.runners.settings import get_settings, RuntimeBudget

    # No auth, account, thread, or model request; refuse outbound proxy traffic.
    for key in tuple(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    (tmp_path / "config.toml").write_text(toml)
    req = request(tmp_path)
    req.update(read_only=read_only, effort=effort)
    settings = replace(
        get_settings(),
        budget=RuntimeBudget(max_tokens=budget, max_turns=2, max_usd=0.25) if budget else None,
    )
    monkeypatch.setenv(settings.env_name("OUTER_WORKER_SANDBOX"), "1" if outer else "0")
    with settings.use():
        config = CodexConfig(
            codex_bin=str(bundled_codex_path()), cwd=str(tmp_path),
            env={"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path)},
            config_overrides=bridge._codex_request_runtime_overrides(req),
        )
        with CodexClient(config) as client:
            client.initialize()
            if accepted:
                bridge._enforce_codex_effective_config(SimpleNamespace(_client=client), req)
            else:
                with pytest.raises(RuntimeError, match="unavailable"):
                    bridge._enforce_codex_effective_config(SimpleNamespace(_client=client), req)
