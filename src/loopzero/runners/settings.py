"""Consumer-injected settings shared by every runner adapter.

Environment names, temporary names, state paths and toolchain paths derive
from one value. ``settings.use()`` scopes unchanged helper signatures;
adapters retain their construction scope. Exported string constants describe
``DEFAULT_SETTINGS`` only. The bridge receives injection across the process
boundary through a consumed, non-secret bootstrap environment value.
"""

from __future__ import annotations

import re
import os
import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Profile

PACKAGED_BRIDGE = Path(__file__).resolve().with_name("bridge.py")
DEFAULT_ENV_PREFIX = "LOOPZERO"
DEFAULT_STATE_ROOT = "~/.local/state/loopzero"
DEFAULT_TEMP_PREFIX = "loopzero"
DEFAULT_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "AGENT_DISPATCH_DEPTH",
        "CODEX_HOME",
        "COLORTERM",
        "FORCE_COLOR",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LOGNAME",
        "LOOPZERO_RUNTIME_SETTINGS",
        "NO_COLOR",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
    }
)
_RUNNER_CHILD_ENV_SUFFIXES = frozenset(
    {
        "CLAUDE_AUTH_FD",
        "CLAUDE_AUTH_STATE",
        "CLAUDE_TOKEN_FILE",
        "CODEX_AUTH_FD",
        "CODEX_AUTH_STATE",
        "CODEX_REFRESH_OUTPUT",
        "CURSOR_AUTH_FD",
        "CURSOR_AUTH_STATE",
        "OUTER_WORKER_SANDBOX",
        "RUNTIME_PROGRESS_FD",
    }
)

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TEMP_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NAME_KIND_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    """Hard per-run limits supplied independently of prompt content."""

    max_tokens: int
    max_turns: int
    max_usd: float

    def __post_init__(self) -> None:
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise TypeError("budget max_tokens must be an integer")
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int):
            raise TypeError("budget max_turns must be an integer")
        if self.max_tokens <= 0 or self.max_turns <= 0:
            raise ValueError("budget token and turn caps must be positive")
        if (
            isinstance(self.max_usd, bool)
            or not isinstance(self.max_usd, (int, float))
            or not math.isfinite(self.max_usd)
            or self.max_usd <= 0
        ):
            raise ValueError("budget USD ceiling must be positive and finite")
        object.__setattr__(self, "max_usd", float(self.max_usd))


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Everything a runner adapter needs to know about its consumer.

    ``toolchain_interpreter`` is the Python that carries the vendor SDKs.  An
    absolute path is used as-is; a relative path is joined to the request's
    tooling root; ``None`` selects the running interpreter.  ``bridge_path``
    follows the same rule and defaults to the packaged bridge.  ``state_root``
    may start with ``~``, which :meth:`state_path` expands against a caller
    supplied home so brokers can keep using the OS account home rather than
    ``$HOME``.
    """

    env_prefix: str = DEFAULT_ENV_PREFIX
    toolchain_interpreter: Path | None = None
    bridge_path: Path = PACKAGED_BRIDGE
    state_root: str | None = None
    temp_prefix: str | None = None
    tooling_root: Path | None = None
    child_env_allowlist: frozenset[str] = DEFAULT_CHILD_ENV_ALLOWLIST
    budget: RuntimeBudget | None = None
    claude_cli_path: Path | None = None
    codex_cli_path: Path | None = None
    cursor_cli_path: Path | None = None
    session_home: Path | None = None

    def __post_init__(self) -> None:
        if self.temp_prefix is None:
            object.__setattr__(self, "temp_prefix", self.env_prefix.lower().replace("_", "-"))
        if self.state_root is None:
            object.__setattr__(self, "state_root", f"~/.local/state/{self.temp_prefix}")
        if not _ENV_NAME_RE.match(self.env_prefix):
            raise ValueError("env_prefix must match [A-Z][A-Z0-9_]*")
        if not _TEMP_PREFIX_RE.match(self.temp_prefix):
            raise ValueError("temp_prefix must match [a-z][a-z0-9-]*")
        if not self.state_root:
            raise ValueError("state_root must be a nonempty path")
        if self.toolchain_interpreter is not None and not isinstance(
            self.toolchain_interpreter, Path
        ):
            raise TypeError("toolchain_interpreter must be a Path or None")
        if not isinstance(self.bridge_path, Path):
            raise TypeError("bridge_path must be a Path")
        if self.budget is not None and not isinstance(self.budget, RuntimeBudget):
            raise TypeError("budget must be a RuntimeBudget or None")
        for name in ("claude_cli_path", "codex_cli_path", "cursor_cli_path"):
            path = getattr(self, name)
            if path is not None and not isinstance(path, Path):
                raise TypeError(f"{name} must be a Path or None")
        if self.session_home is not None:
            if not isinstance(self.session_home, Path):
                raise TypeError("session_home must be a Path or None")
            if not self.session_home.is_absolute():
                raise ValueError("session_home must be absolute")
        if not isinstance(self.child_env_allowlist, frozenset) or any(
            not isinstance(name, str) or not _ENV_NAME_RE.match(name)
            for name in self.child_env_allowlist
        ):
            raise TypeError("child_env_allowlist must be a frozenset of environment names")
        object.__setattr__(
            self,
            "child_env_allowlist",
            self.child_env_allowlist
            | {f"{self.env_prefix}_{suffix}" for suffix in _RUNNER_CHILD_ENV_SUFFIXES},
        )

    @classmethod
    def from_profile(cls, profile: Profile) -> RuntimeSettings:
        """Derive settings from a validated ``workflow.toml`` profile.

        ``[package].env_prefix`` and ``[package].state_root`` map directly;
        ``[toolchain].interpreter`` (a path, relative to the consumer's
        tooling root unless absolute) selects the SDK interpreter.  The bridge
        is always the packaged one and the temp prefix is the lower-cased
        environment prefix.
        """
        interpreter = profile.toolchain.get("interpreter")
        if interpreter is not None and (not isinstance(interpreter, str) or not interpreter):
            raise ValueError("[toolchain].interpreter must be a nonempty path string")
        return cls(
            env_prefix=profile.env_prefix,
            toolchain_interpreter=Path(interpreter) if interpreter else None,
            state_root=profile.state_root if profile.state_root_explicit else None,
            tooling_root=profile.root.resolve(),
            temp_prefix=profile.env_prefix.lower().replace("_", "-"),
        )

    @contextmanager
    def use(self):
        """Scope unchanged runner/helper signatures to this consumer.

        Context-local values keep independent threads and asyncio tasks isolated.
        Adapters constructed in this scope retain the settings for later calls.
        """
        token = _SETTINGS.set(self)
        try:
            yield self
        finally:
            _SETTINGS.reset(token)

    def child_environment(self) -> dict[str, str]:
        """Serialize non-secret path/name injection across the bridge boundary."""
        return {SETTINGS_ENV: json.dumps({
            "env_prefix": self.env_prefix,
            "toolchain_interpreter": str(self.toolchain_interpreter) if self.toolchain_interpreter else None,
            "bridge_path": str(self.bridge_path),
            "state_root": self.state_root,
            "temp_prefix": self.temp_prefix,
            "tooling_root": str(self.tooling_root) if self.tooling_root else None,
            "child_env_allowlist": sorted(self.child_env_allowlist),
            "budget": (
                {
                    "max_tokens": self.budget.max_tokens,
                    "max_turns": self.budget.max_turns,
                    "max_usd": self.budget.max_usd,
                }
                if self.budget is not None
                else None
            ),
            "claude_cli_path": str(self.claude_cli_path) if self.claude_cli_path else None,
            "codex_cli_path": str(self.codex_cli_path) if self.codex_cli_path else None,
            "cursor_cli_path": str(self.cursor_cli_path) if self.cursor_cli_path else None,
            "session_home": str(self.session_home) if self.session_home else None,
        }, separators=(",", ":"))}

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        """Read the parent-owned bridge bootstrap, consuming it before SDK launch."""
        raw = os.environ.pop(SETTINGS_ENV, None)
        if raw is None:
            return cls()
        values = json.loads(raw)
        for key in (
            "toolchain_interpreter", "bridge_path", "tooling_root",
            "claude_cli_path", "codex_cli_path", "cursor_cli_path", "session_home",
        ):
            if values.get(key) is not None:
                values[key] = Path(values[key])
        if values.get("budget") is not None:
            values["budget"] = RuntimeBudget(**values["budget"])
        if "child_env_allowlist" in values:
            values["child_env_allowlist"] = frozenset(values["child_env_allowlist"])
        return cls(**values)

    # Environment and file-name derivation -------------------------------

    def env_name(self, suffix: str) -> str:
        """Return ``<env_prefix>_<suffix>`` for one runner-owned variable."""
        if not _ENV_NAME_RE.match(suffix):
            raise ValueError("environment suffix must match [A-Z][A-Z0-9_]*")
        return f"{self.env_prefix}_{suffix}"

    def temp_name(self, kind: str) -> str:
        """Return the ``prefix=`` argument for one kind of temporary path."""
        return f"{self.memfd_name(kind)}-"

    def memfd_name(self, kind: str) -> str:
        """Return a content-free label for a sealed memory descriptor."""
        if not _NAME_KIND_RE.match(kind):
            raise ValueError("temporary name kind must match [a-z][a-z0-9-]*")
        return f"{self.temp_prefix}-{kind}"

    def lock_name(self, kind: str) -> str:
        """Return the hidden lock file name for one broker."""
        if not _NAME_KIND_RE.match(kind):
            raise ValueError("lock name kind must match [a-z][a-z0-9-]*")
        return f".{self.temp_prefix.replace('-', '_')}_{kind.replace('-', '_')}.lock"

    def state_path(self, home: Path) -> Path:
        """Expand ``state_root`` against ``home`` (never ``$HOME``)."""
        root = self.state_root
        if root == "~":
            return home
        if root.startswith("~/"):
            return home / root[2:]
        path = Path(root)
        return path if path.is_absolute() else home / path

    def workspace_root(self, tooling_root: Path) -> Path:
        """Return the private-state parent for per-run adapter workspaces.

        Runner scratch space must remain writable when the tooling checkout is
        mounted read-only.  Resolve ``~`` against the OS account home rather
        than the child ``HOME`` environment, matching credential-broker state.
        ``tooling_root`` remains in the signature for compatibility with the
        other path resolvers; it is deliberately not used as writable storage.
        """
        del tooling_root
        try:
            import pwd

            account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):  # pragma: no cover - non-POSIX
            account_home = Path.home()
        return self.state_path(account_home) / f".{self.temp_prefix}-runner-workspaces"

    # Toolchain resolution ------------------------------------------------

    def _tooling_anchor(self) -> str | None:
        interpreter = self.toolchain_interpreter
        if interpreter is None or interpreter.is_absolute() or len(interpreter.parts) < 2:
            return None
        return interpreter.parts[0]

    def repository_root(self, tooling_root: Path) -> Path:
        """Normalize a tooling checkout or its toolchain directory.

        A caller may point at the directory that holds the interpreter (the
        first component of a relative ``toolchain_interpreter``) instead of
        the checkout; that directory is normalized to its parent.
        """
        candidate_root = tooling_root
        anchor = self._tooling_anchor()
        if anchor is not None and candidate_root.name == anchor:
            candidate_root = candidate_root.parent
        return candidate_root

    def interpreter(self, tooling_root: Path) -> Path:
        """Resolve the SDK interpreter for a request's tooling root."""
        interpreter = self.toolchain_interpreter
        if interpreter is None:
            return Path(sys.executable)
        if interpreter.is_absolute():
            return interpreter
        return self.repository_root(tooling_root) / interpreter

    def bridge(self, tooling_root: Path) -> Path:
        """Resolve the bridge script for a request's tooling root."""
        path = self.bridge_path
        if not path.is_absolute():
            path = self.repository_root(tooling_root) / path
        return path.resolve(strict=False)

    def bridge_command(self, tooling_root: Path) -> list[str]:
        """Return the isolated-mode bridge launch command."""
        return [str(self.interpreter(tooling_root)), "-I", str(self.bridge(tooling_root))]

    def cli(self, vendor: str, fallback: str) -> str:
        """Return the explicitly selected executable, or a legacy command name."""
        paths = {
            "claude": self.claude_cli_path,
            "codex": self.codex_cli_path,
            "cursor": self.cursor_cli_path,
        }
        try:
            path = paths[vendor]
        except KeyError as exc:
            raise ValueError(f"unsupported runtime executable {vendor!r}") from exc
        return str(path) if path is not None else fallback


DEFAULT_SETTINGS = RuntimeSettings()
# Fixed transport bootstrap key; all consumer-owned environment names derive
# from env_prefix. This key is consumed before entering the vendor SDK.
SETTINGS_ENV = "LOOPZERO_RUNTIME_SETTINGS"
_SETTINGS: ContextVar[RuntimeSettings] = ContextVar("runtime_settings", default=DEFAULT_SETTINGS)


def get_settings() -> RuntimeSettings:
    return _SETTINGS.get()


def using_adapter_settings(method):
    """Retain constructor-scope injection without changing method signatures."""
    @wraps(method)
    def scoped(self, *args, **kwargs):
        with self._settings.use():
            return method(self, *args, **kwargs)
    return scoped


__all__ = [
    "DEFAULT_ENV_PREFIX",
    "DEFAULT_CHILD_ENV_ALLOWLIST",
    "DEFAULT_SETTINGS",
    "DEFAULT_STATE_ROOT",
    "DEFAULT_TEMP_PREFIX",
    "PACKAGED_BRIDGE",
    "RuntimeBudget",
    "RuntimeSettings",
]
