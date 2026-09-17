import pytest

from loopzero.kernel import run_identity

RUN = 'sr_' + '1' * 32
SELECTION = {
    'artifact_id': '1' * 64, 'manifest_sha256': '2' * 64,
    'package_revision': '3' * 40, 'consumer_revision': '4' * 40,
    'policy_sha256': '5' * 64, 'core_tree': '6' * 40,
    'python_sha256': '7' * 64,
}


def test_original_selection_survives_reentry_and_cannot_be_replaced():
    original = {'run_id': RUN}
    run_identity.bind_delivery_contract(original, [], start=True,
                                       runtime_artifact=SELECTION)
    resumed = {'run_id': RUN}
    run_identity.bind_delivery_contract(resumed, [original], start=True)
    assert resumed['runtime_artifact'] == SELECTION
    assert run_identity.run_runtime_artifact([original, resumed], RUN) == SELECTION
    with pytest.raises(ValueError, match='artifact'):
        run_identity.bind_delivery_contract({'run_id': RUN}, [original], start=True,
                                            runtime_artifact=SELECTION | {'artifact_id': '9' * 64})


def test_artifact_cannot_be_retrofitted_to_historical_run():
    with pytest.raises(ValueError, match='artifact'):
        run_identity.bind_delivery_contract({'run_id': RUN}, [{'run_id': RUN}],
                                            runtime_artifact=SELECTION)
    assert run_identity.run_runtime_artifact([{'run_id': RUN}], RUN) is None


@pytest.mark.parametrize('selection', [SELECTION | {'artifact_id': '../../foreign'},
                                      SELECTION | {'unknown': 'field'},
                                      SELECTION | {'manifest_sha256': None}])
def test_malformed_artifact_identity_fails_closed(selection):
    with pytest.raises(ValueError, match='artifact'):
        run_identity.bind_delivery_contract({'run_id': RUN}, [], start=True,
                                            runtime_artifact=selection)


def test_later_row_cannot_change_original_runtime_artifact():
    rows = [{'run_id': RUN, 'runtime_artifact': SELECTION},
            {'run_id': RUN, 'runtime_artifact': SELECTION | {'core_tree': '8' * 40}}]
    with pytest.raises(ValueError, match='artifact'):
        run_identity.run_runtime_artifact(rows, RUN)


def test_resume_rejects_replacement_before_appending(tmp_path, monkeypatch, capsys):
    import sys

    from loopzero.kernel import run_log
    monkeypatch.setattr(run_log, 'repo_root', lambda: tmp_path)
    monkeypatch.setattr(run_log, 'load_entries', lambda _: [
        {'run_id': RUN, 'runtime_artifact': SELECTION}])
    monkeypatch.setattr(sys, 'argv', ['skill_run_log', '--resume', '--run-id', RUN,
                                    '--skill', 'work-issue', '--notes', 'resume original'])
    with pytest.raises(SystemExit) as caught:
        run_log.main(runtime_artifact=SELECTION | {'artifact_id': '9' * 64})
    assert caught.value.code == 2
    assert 'runtime artifact cannot change' in capsys.readouterr().err
    assert not list(tmp_path.rglob('*.jsonl'))


def test_incompatible_shared_state_is_rejected_without_mutation(tmp_path, monkeypatch):
    from loopzero.kernel import authority_store
    from loopzero.kernel.authority_store import DispatchError
    monkeypatch.setattr(authority_store, '_authority_repository_binding', lambda _: 'a' * 64)
    monkeypatch.setattr(authority_store, 'load_coordinator_ledger_state',
                        lambda _: {'scheme': 'future-incompatible-state'})
    marker = tmp_path / 'preserved'
    marker.write_bytes(b'original state')
    before = [(p.relative_to(tmp_path), p.read_bytes()) for p in tmp_path.rglob('*') if p.is_file()]
    with pytest.raises(DispatchError):
        authority_store.load_authority_snapshot(tmp_path)
    assert [(p.relative_to(tmp_path), p.read_bytes()) for p in tmp_path.rglob('*') if p.is_file()] == before
