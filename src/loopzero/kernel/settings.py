"""Consumer configuration for Linux kernel mechanisms.

Configure once, before importing kernel mechanisms in a coordinator process.
Subprocess entry points select a namespace with LOOPZERO_ENV_PREFIX. Privileged
callers must supply settings from an approved Profile, never candidate hooks.
"""
from __future__ import annotations

import os
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loopzero.config import Profile


@dataclass(frozen=True)
class KernelSettings:
    env_prefix: str = "LOOPZERO"
    audit_root: Path = Path(".audit")
    state_root: Path | None = None
    temp_prefix: str | None = None
    lease_lock_name: str = "worktree-boundary.lock"
    lease_owner_name: str = "worktree-boundary.owner.json"
    authority_lock_name: str | None = None
    coordinator_lock_name: str = ".coordinator-key.lock"
    ledger_state_lock_name: str = ".ledger-state.lock"
    event_lock_name: str = ".write.lock"
    closeout_lock_name: str = "delivery-closeout.lock"
    downgrade_barrier_name: str = "1970-01-01.jsonl"
    sandbox_node_root: Path = Path("/run/guardian-node")
    sandbox_bin_root: Path = Path("/run/guardian-bin")
    sandbox_corepack_home: Path = Path("/run/guardian-corepack-home")
    sandbox_home: Path = Path("/tmp/guardian-home")
    sandbox_passthrough: frozenset[str] = frozenset({"LANG", "LC_ALL", "TERM", "TZ"})
    policy_version: str = "2026-08-17-v11"
    telemetry_schema_version: str = "dispatch-telemetry-v9"
    ledger_domain: bytes | None = None
    legacy_signature_namespace: str | None = None
    legacy_public_key: str | None = None
    ledger_max_bytes: int = 16 * 1024 * 1024
    ledger_max_records: int = 20_000
    phase_skills: frozenset[str] = frozenset({"work-issue", "execute-blueprint"})
    legacy_contract: str = "intelflo-v1"
    worktree_tags: dict[str, str] = field(default_factory=lambda: {
        ".claude/worktrees": "claude-code", "~/.cursor": "cursor/codex", ".worktrees": "manual"})
    worktree_markers: dict[str, str] = field(default_factory=lambda: {"/.cursor/worktrees/": "cursor/codex"})
    prunable_worktree_tags: frozenset[str] = frozenset({"claude-code"})
    toolchain: dict = field(default_factory=dict)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.env_prefix):
            raise ValueError("invalid kernel environment prefix")
        slug = self.env_prefix.lower()
        object.__setattr__(self, "ledger_domain", self.ledger_domain or f"{slug}-dispatch-ledger-v3\0".encode())
        object.__setattr__(self, "legacy_signature_namespace", self.legacy_signature_namespace or f"{slug}-dispatch-coordinator")
        object.__setattr__(self, "audit_root", Path(self.audit_root))
        object.__setattr__(self, "state_root", Path(self.state_root or f"~/.local/state/{slug}"))
        object.__setattr__(self, "temp_prefix", self.temp_prefix or f"{slug}-")
        object.__setattr__(self, "authority_lock_name", self.authority_lock_name or f"{slug}-authority-ledger.lock")

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"

    def account_state_root(self, account_home: Path) -> Path:
        """Expand '~' against passwd, never the caller's untrusted HOME."""
        parts = self.state_root.parts
        return account_home.joinpath(*parts[1:]) if parts[0] == "~" else self.state_root

    @classmethod
    def from_profile(cls, profile: Profile) -> KernelSettings:
        return cls(env_prefix=profile.env_prefix, audit_root=profile.audit_root,
                   state_root=Path(profile.state_root), toolchain=dict(profile.toolchain))


    def child_environment(self) -> dict[str, str]:
        """Serialize approved settings for installed-package child entry points."""
        data = asdict(self)
        for name, value in list(data.items()):
            if isinstance(value, Path):
                data[name] = str(value)
            elif isinstance(value, bytes):
                data[name] = value.hex()
            elif isinstance(value, frozenset):
                data[name] = sorted(value)
        return {"LOOPZERO_ENV_PREFIX": self.env_prefix, "LOOPZERO_AUDIT_ROOT": str(self.audit_root),
                "LOOPZERO_KERNEL_SETTINGS": json.dumps(data)}

    @classmethod
    def from_environment(cls) -> KernelSettings:
        payload = os.environ.get("LOOPZERO_KERNEL_SETTINGS")
        if not payload:
            return cls(env_prefix=os.environ.get("LOOPZERO_ENV_PREFIX", "LOOPZERO"))
        data = json.loads(payload)
        for name in ("sandbox_node_root", "sandbox_bin_root", "sandbox_corepack_home", "sandbox_home"):
            if name in data:
                data[name] = Path(data[name])
        for name in ("sandbox_passthrough", "phase_skills", "prunable_worktree_tags"):
            if name in data:
                data[name] = frozenset(data[name])
        if "ledger_domain" in data:
            data["ledger_domain"] = bytes.fromhex(data["ledger_domain"])
        return cls(**data)


settings = KernelSettings.from_environment()


def configure(value: KernelSettings) -> None:
    """Initialize the process before importing the mechanisms that consume it."""
    global settings
    settings = value
