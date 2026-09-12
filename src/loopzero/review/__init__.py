"""Portable review mechanisms."""

from dataclasses import replace
from pathlib import Path
from threading import Lock

from . import acceptance, authority, chain, evidence, findings, harness, preflight, risk, routing
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
            raise ConfigError([
                "review is already configured with a different Profile"
            ])
        kernel_settings.configure(_merged_kernel_settings(profile))
        for mechanism in (acceptance, chain, evidence, findings, risk, routing):
            mechanism.configure(profile)
        harness.configure(profile, append_authority=append_authoritative_record)
        authority.configure(
            uncarryable_delta_authority_is_valid=uncarryable_delta_authority_is_valid
        )
        authority.configure_kernel_seams()
        _DEFAULT_PROFILE = profile

__all__ = [
    "acceptance", "authority", "chain", "evidence", "findings", "harness",
    "preflight", "risk", "routing", "configure"
]
