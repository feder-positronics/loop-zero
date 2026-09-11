"""Named consumer injection points for workstream A4.

Register trusted callables before using review-aware projections. Missing
adapters fail closed; the kernel never discovers code in a candidate worktree.
"""
from collections.abc import Callable

class MissingAdapter(RuntimeError):
    pass

_adapters: dict[str, Callable] = {}

def configure(**adapters: Callable) -> None:
    unknown = adapters.keys() - ADAPTER_NAMES
    if unknown:
        raise ValueError(f"unknown kernel adapters: {sorted(unknown)}")
    if any(not callable(adapter) for adapter in adapters.values()):
        raise TypeError("kernel adapters must be callable")
    _adapters.update(adapters)

def _invoke(name, *args, **kwargs):
    if name not in _adapters:
        raise MissingAdapter(f"A4 adapter required: {name}")
    return _adapters[name](*args, **kwargs)

ADAPTER_NAMES = frozenset(['_latest_attempt_settlement_indices', 'accepted_review_terminals', 'archived_supersession_deposits', 'authenticated_retry_outcomes', 'authenticated_review_terminals', 'authenticated_supersessions', 'authenticated_verdicts', 'delivery_controller_records', 'latest_explicit_alias_availability', 'load_authority_records', 'passing_archive_ancestry', 'passing_archive_anchor', 'supersession_reason_matches_terminal', 'validate_archived_review_witness'])

def _latest_attempt_settlement_indices(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('_latest_attempt_settlement_indices', *args, **kwargs)

def accepted_review_terminals(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('accepted_review_terminals', *args, **kwargs)

def archived_supersession_deposits(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('archived_supersession_deposits', *args, **kwargs)

def authenticated_retry_outcomes(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('authenticated_retry_outcomes', *args, **kwargs)

def authenticated_review_terminals(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('authenticated_review_terminals', *args, **kwargs)

def authenticated_supersessions(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('authenticated_supersessions', *args, **kwargs)

def authenticated_verdicts(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('authenticated_verdicts', *args, **kwargs)

def delivery_controller_records(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('delivery_controller_records', *args, **kwargs)

def latest_explicit_alias_availability(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('latest_explicit_alias_availability', *args, **kwargs)

def load_authority_records(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('load_authority_records', *args, **kwargs)

def passing_archive_ancestry(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('passing_archive_ancestry', *args, **kwargs)

def passing_archive_anchor(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('passing_archive_anchor', *args, **kwargs)

def supersession_reason_matches_terminal(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('supersession_reason_matches_terminal', *args, **kwargs)

def validate_archived_review_witness(*args, **kwargs):
    # TODO(A4): inject the consumer review/delivery mechanism.
    return _invoke('validate_archived_review_witness', *args, **kwargs)
