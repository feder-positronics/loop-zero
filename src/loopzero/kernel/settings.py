"""Consumer configuration for Linux kernel mechanisms.

Configure once, before importing kernel mechanisms in a coordinator process.
Subprocess entry points select a namespace with LOOPZERO_ENV_PREFIX. Privileged
callers must supply settings from an approved Profile, never candidate hooks.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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
    ledger_domain: bytes = b"intelflo-dispatch-ledger-v3\0"
    legacy_signature_namespace: str = "intelflo-dispatch-coordinator"
    legacy_public_key: str | None = None
    ledger_max_bytes: int = 16 * 1024 * 1024
    ledger_max_records: int = 20_000
    phase_skills: frozenset[str] = frozenset({"work-issue", "execute-blueprint"})
    toolchain: dict = field(default_factory=dict)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.env_prefix):
            raise ValueError("invalid kernel environment prefix")
        slug = self.env_prefix.lower()
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


settings = KernelSettings(env_prefix=os.environ.get("LOOPZERO_ENV_PREFIX", "LOOPZERO"))


def configure(value: KernelSettings) -> None:
    """Initialize the process before importing the mechanisms that consume it."""
    global settings
    settings = value
