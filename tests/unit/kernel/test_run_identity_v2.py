import pytest

from loopzero.kernel import run_identity as identity

RUN = 'sr_' + '1' * 32


def test_v2_selected_only_for_genuinely_new_start():
    new = {'run_id': RUN}
    identity.bind_delivery_contract(new, [], start=True, new_task_contract='loop-zero-v2')
    assert identity.uses_pr_review([new], RUN)


@pytest.mark.parametrize('contract', [None, 'intelflo-v1', 'loop-zero-v1', 'loop-zero-v2'])
def test_v2_adoption_never_migrates_an_original_run(contract):
    original = {'run_id': RUN}
    if contract is not None:
        original['delivery_contract'] = contract
    next_row = {'run_id': RUN}
    identity.bind_delivery_contract(next_row, [original], start=True,
                                    new_task_contract='loop-zero-v2')
    assert next_row['delivery_contract'] == (contract or 'intelflo-v1')
    assert identity.uses_pr_review([original, next_row], RUN) == (contract == 'loop-zero-v2')


def test_missing_history_terminal_does_not_gain_v2():
    row = {'run_id': RUN}
    identity.bind_delivery_contract(row, [], new_task_contract='loop-zero-v2')
    assert row['delivery_contract'] == 'intelflo-v1'


def test_v2_policy_rejects_identity_change():
    with pytest.raises(ValueError, match='contract'):
        identity.uses_pr_review([{'run_id': RUN, 'delivery_contract': 'loop-zero-v1'},
                                {'run_id': RUN, 'delivery_contract': 'loop-zero-v2'}], RUN)


def test_unknown_new_contract_is_rejected():
    with pytest.raises(ValueError, match='contract'):
        identity.bind_delivery_contract({'run_id': RUN}, [], start=True,
                                        new_task_contract='invented')


def test_trusted_v2_entry_selects_new_contract_and_preserves_resumed_v1(tmp_path, monkeypatch):
    import json

    from loopzero.kernel import run_log
    monkeypatch.setattr(run_log, 'repo_root', lambda: tmp_path)
    monkeypatch.setattr(run_log, 'common_fields', lambda: {
        'ts': '2026-09-17T12:00:00Z', 'session_id': 'session',
        'session_source': 'test', 'harness': 'test', 'git_branch': 'feature/new',
    })
    monkeypatch.setattr('sys.argv', ['run-log', '--start', '--skill', 'work-issue'])
    assert run_log.main(new_task_contract='loop-zero-v2') == 0
    rows = run_log.load_entries(tmp_path / run_log.settings.audit_root / 'skill-runs')
    assert len(rows) == 1 and rows[0]['delivery_contract'] == 'loop-zero-v2'
    # Even an older caller's default cannot rewrite the established v2 identity.
    assert run_log.main() == 0
    assert run_log.load_entries(tmp_path / run_log.settings.audit_root / 'skill-runs') == rows
    old = dict(rows[0], run_id=RUN, git_branch='feature/old', delivery_contract='loop-zero-v1')
    log = tmp_path / run_log.settings.audit_root / 'skill-runs' / '2026-09-16.jsonl'
    log.write_text(json.dumps(old) + '\n')
    monkeypatch.setattr('sys.argv', ['run-log', '--start', '--skill', 'work-issue',
                                   '--git-branch', 'feature/old'])
    assert run_log.main(new_task_contract='loop-zero-v2') == 0
    rows = run_log.load_entries(tmp_path / run_log.settings.audit_root / 'skill-runs')
    assert identity.run_delivery_contract(rows, RUN) == 'loop-zero-v1'
