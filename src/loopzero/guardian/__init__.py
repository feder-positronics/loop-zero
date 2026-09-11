"""Optional generic Guardian state, deposit, risk and ratchet mechanisms."""

from . import deposit, ratchet, risk, state


def configure(*, append_authority_records) -> None:
    """Bind Guardian seams owned by the excluded consumer dispatcher."""
    deposit.configure(append_authority_records=append_authority_records)

__all__ = ["deposit", "ratchet", "risk", "state", "configure"]
