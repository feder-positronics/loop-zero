"""Portable review mechanisms."""

from dataclasses import replace
from importlib import import_module
import sys
from pathlib import Path
from threading import Lock

from ..config import ConfigError, Profile
from ..kernel import settings as kernel_settings

_DEFAULT_PROFILE: Profile | None = None
_CONFIGURE_LOCK = Lock()


def _merged_kernel_settings(profile: Profile) -> kernel_settings.KernelSettings:
    """Merge profile defaults below a consumer-pinned absolute interpreter.

    The profile supplies the complete kernel-settings base.  When the active
    settings belong to the same consumer namespace, an absolute interpreter
    explicitly resolved by that consumer wins over the profile's toolchain
    interpreter.  No other active-process setting overrides the profile.
    """
    configured = kernel_settings.KernelSettings.from_profile(profile)
    active = kernel_settings.settings
    interpreter = active.toolchain.get("interpreter")
    if (
        active.env_prefix == configured.env_prefix
        and isinstance(interpreter, (str, Path))
        and Path(interpreter).is_absolute()
    ):
        toolchain = dict(configured.toolchain)
        toolchain["interpreter"] = interpreter
        configured = replace(configured, toolchain=toolchain)
    return configured


def configure(
    profile: Profile,
    *,
    uncarryable_delta_authority_is_valid=None,
    append_authoritative_record=None,
) -> None:
    """Configure one process profile and bind its declared kernel seams."""
    global _DEFAULT_PROFILE
    with _CONFIGURE_LOCK:
        if _DEFAULT_PROFILE is not None and profile != _DEFAULT_PROFILE:
            raise ConfigError(["review is already configured with a different Profile"])
        configured = _merged_kernel_settings(profile)
        # Mechanisms retain settings and derive constants during import. Never
        # replace those values underneath an already imported consumer.
        for name, module in tuple(sys.modules.items()):
            if (
                not name.startswith("loopzero.kernel.")
                or module is kernel_settings
                or module is None
            ):
                continue
            captured = vars(module).get("settings")
            if (
                isinstance(captured, kernel_settings.KernelSettings)
                and captured != configured
            ):
                raise ConfigError(
                    [
                        "configure review before importing kernel mechanisms; "
                        "a loaded mechanism already captured different settings"
                    ]
                )
        kernel_settings.configure(configured)
        from . import (
            acceptance,
            authority,
            chain,
            evidence,
            findings,
            harness,
            provisional_findings,
            risk,
            routing,
        )

        for mechanism in (
            acceptance,
            chain,
            evidence,
            findings,
            provisional_findings,
            risk,
            routing,
        ):
            mechanism.configure(profile)
        harness.configure(profile, append_authority=append_authoritative_record)
        authority.configure(
            uncarryable_delta_authority_is_valid=uncarryable_delta_authority_is_valid
        )
        authority.configure_kernel_seams()
        _DEFAULT_PROFILE = profile


__all__ = [
    "acceptance",
    "admission",
    "authority",
    "chain",
    "configure",
    "evidence",
    "findings",
    "harness",
    "preflight",
    "provisional_findings",
    "risk",
    "routing",
    "stats",
    "telemetry",
    "trust_claims",
]


def __getattr__(name: str):
    """Load public mechanisms on demand, after callers can configure the kernel."""
    if name in __all__ and name != "configure":
        return import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
