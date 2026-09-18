"""Bounded historical prefix reuse must preserve authentication semantics."""
import pytest

from loopzero.kernel import authority_projection as projection


@pytest.fixture(autouse=True)
def isolated_cache():
    projection._authentication_cache.clear()
    yield
    projection._authentication_cache.clear()


def counted_scans(monkeypatch):
    calls = []
    original = projection._authenticate_coordinator_record_ids_uncached

    def counted(records):
        calls.append(tuple(map(id, records)))
        return original(records)

    monkeypatch.setattr(projection, '_authenticate_coordinator_record_ids_uncached', counted)
    return calls


def invalid_record(number):
    return {'task_id': str(number), 'terminal_authority_proof':
            {'authority_kind': 'coordinator', 'scheme': 'invalid-proof'}}


def test_recursive_historical_prefix_working_set_is_authenticated_once(monkeypatch):
    calls = counted_scans(monkeypatch)
    records = [invalid_record(number) for number in range(11)]
    # The installed retained history needs eleven distinct recursive prefixes.
    # Each pass recreates views, retaining the same exact record identities.
    for _ in range(3):
        for stop in range(1, 12):
            assert projection._authenticated_coordinator_record_ids(records[:stop]) == frozenset()
    assert len(calls) == 11


def test_cache_remains_bounded_and_eviction_reauthenticates_only_evicted_history(monkeypatch):
    calls = counted_scans(monkeypatch)
    limit = projection._AUTHENTICATION_CACHE_LIMIT
    histories = [[invalid_record(number)] for number in range(limit + 1)]
    for history in histories:
        assert projection._authenticated_coordinator_record_ids(history) == frozenset()
    assert len(projection._authentication_cache) == limit
    for history in histories[1:]:
        assert projection._authenticated_coordinator_record_ids(history) == frozenset()
        pinned, _ = projection._authentication_cache[projection._authentication_cache_key(history)]
        assert pinned[0][0] is history[0]
        assert pinned[1] is history
    assert len(calls) == limit + 1
    projection._authenticated_coordinator_record_ids(histories[0])
    assert len(calls) == limit + 2
    assert len(projection._authentication_cache) == limit


def test_changed_identity_order_append_and_trust_fields_do_not_reuse_authentication(monkeypatch):
    calls = counted_scans(monkeypatch)
    records = [invalid_record(1), invalid_record(2)]
    projection._authenticated_coordinator_record_ids(records)
    projection._authenticated_coordinator_record_ids(list(records))
    assert len(calls) == 1
    for changed in (list(reversed(records)), [dict(row) for row in records],
                    [*records, invalid_record(3)],
                    projection.AuthorityRecordView(records, trusted_retained_ids=(id(records[0]),)),
                    projection.AuthorityRecordView(records, checkpoint_prefix={'record_count': 2})):
        projection._authenticated_coordinator_record_ids(changed)
    assert len(calls) == 6


def test_transient_authentication_failure_is_retried_but_invalid_proof_stays_rejected(monkeypatch):
    calls = []

    def verify(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise projection.TerminalAuthorityOperationalError('temporary provider failure')
        raise projection.TerminalAuthorityError('invalid proof')

    monkeypatch.setattr(projection, 'verify_terminal_authority', verify)
    records = [invalid_record(1)]
    assert projection._authenticated_coordinator_record_ids(records) == frozenset()
    assert not projection._authentication_cache
    assert projection._authenticated_coordinator_record_ids(records) == frozenset()
    assert projection._authenticated_coordinator_record_ids(records) == frozenset()
    assert len(calls) == 2
