"""Closed construction registry for the supported native runtimes.

Routing, retry, lifecycle, and worker-result policy deliberately live above
this module.  The registry only binds a selected vendor to its native adapter
and declares which normalized transport is preferred versus a same-vendor CLI
fallback.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from .claude import CLAUDE_CLI_TRANSPORT, CLAUDE_SDK_TRANSPORT, ClaudeAdapter
from .codex import CODEX_CLI_TRANSPORT, CODEX_SDK_TRANSPORT, CodexAdapter
from .contract import RuntimeAdapter
from .cursor import CursorAdapter
from .fake import FakeAdapter
from .settings import RuntimeSettings

RuntimeVendor = Literal["claude", "codex", "cursor", "fake"]


@dataclass(frozen=True, slots=True)
class RuntimeRegistration:
    """Transport facts and one constructor for a supported native vendor."""

    preferred_transport: str
    cli_fallback_transport: str | None
    factory: Callable[..., RuntimeAdapter]


class RuntimeRegistry:
    """Fail-closed adapter registry; unknown vendor names are never routed."""

    def __init__(self, registrations: dict[RuntimeVendor, RuntimeRegistration]) -> None:
        self._registrations = MappingProxyType(dict(registrations))

    @property
    def vendors(self) -> frozenset[str]:
        return frozenset(self._registrations)

    def registration(self, vendor: str) -> RuntimeRegistration:
        if vendor not in {"claude", "codex", "cursor", "fake"}:
            raise ValueError(f"unsupported native runtime {vendor!r}")
        try:
            return self._registrations[cast(RuntimeVendor, vendor)]
        except KeyError as exc:
            raise ValueError(f"unsupported native runtime {vendor!r}") from exc

    def create(self, vendor: str, **kwargs: Any) -> RuntimeAdapter:
        """Construct the selected vendor adapter with dispatcher-owned seams."""
        settings = kwargs.pop("settings", None)
        if settings is None:
            return self.registration(vendor).factory(**kwargs)
        if not isinstance(settings, RuntimeSettings):
            raise TypeError("settings must be RuntimeSettings")
        with settings.use():
            return self.registration(vendor).factory(**kwargs)


def _create_claude_adapter(**kwargs: Any) -> RuntimeAdapter:
    return ClaudeAdapter(**kwargs)


def _create_codex_adapter(**kwargs: Any) -> RuntimeAdapter:
    return CodexAdapter(**kwargs)


def _create_cursor_adapter(**kwargs: Any) -> RuntimeAdapter:
    return CursorAdapter(**kwargs)


NATIVE_RUNTIME_REGISTRY = RuntimeRegistry(
    {
        "claude": RuntimeRegistration(
            preferred_transport=CLAUDE_SDK_TRANSPORT,
            cli_fallback_transport=CLAUDE_CLI_TRANSPORT,
            factory=_create_claude_adapter,
        ),
        "codex": RuntimeRegistration(
            preferred_transport=CODEX_SDK_TRANSPORT,
            cli_fallback_transport=CODEX_CLI_TRANSPORT,
            factory=_create_codex_adapter,
        ),
        "cursor": RuntimeRegistration(
            preferred_transport="cursor/cli-stream-json",
            cli_fallback_transport=None,
            factory=_create_cursor_adapter,
        ),
    }
)


# Keep the legacy native registry closed to its original three vendors.
RUNTIME_REGISTRY = RuntimeRegistry({
    **{vendor: NATIVE_RUNTIME_REGISTRY.registration(vendor)
       for vendor in NATIVE_RUNTIME_REGISTRY.vendors},
    "fake": RuntimeRegistration("fake/scenario", None, FakeAdapter),
})
