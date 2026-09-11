"""Portable review mechanisms."""

from . import acceptance, authority, chain, evidence, findings, harness, preflight, risk, routing
from ..config import Profile
from ..kernel import settings as kernel_settings


def configure(profile: Profile, *, uncarryable_delta_authority_is_valid=None) -> None:
    """Configure all review mechanisms and bind their declared kernel seams."""
    kernel_settings.configure(kernel_settings.KernelSettings.from_profile(profile))
    for mechanism in (acceptance, chain, evidence, findings, risk, routing):
        mechanism.configure(profile)
    authority.configure(
        uncarryable_delta_authority_is_valid=uncarryable_delta_authority_is_valid
    )
    authority.configure_kernel_seams()

__all__ = [
    "acceptance", "authority", "chain", "evidence", "findings", "harness",
    "preflight", "risk", "routing", "configure"
]
