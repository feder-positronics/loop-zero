from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.delivery import draft
from loopzero.delivery.publish import PublicationError

SHA = 'a' * 40
BASE = 'b' * 40


def request(**changes):
    values = {'title': 'Draft', 'body': 'Objective and acceptance', 'head': 'feature/task',
                  'base': 'main', 'expected_head': SHA, 'expected_base': BASE,
                  'expected_repository': 'owner/project', 'run_id': 'sr_' + '1' * 32}
    return draft.DraftPublicationRequest(**(values | changes))


def pr(**changes):
    return {'number': 42, 'html_url': 'https://github.com/owner/project/pull/42',
                'state': 'open', 'draft': True, 'body': '<!-- skill-run-id: sr_' + '1' * 32 + ' -->',
                'head': {'ref': 'feature/task', 'sha': SHA,
                      'repo': {'full_name': 'owner/project'}},
                'base': {'ref': 'main', 'sha': BASE,
                      'repo': {'full_name': 'owner/project'}}} | changes


class Remote:
    def __init__(self, existing=(), created=None):
        self.existing = list(existing)
        self.created = created or pr()
        self.calls = []
        self.failure = False
        self.live = None

    def run_json(self, args, *, payload=None):
        self.calls.append((args, payload))
        endpoint = args[-1]
        if endpoint == 'repos/{owner}/{repo}':
            return {'full_name': 'owner/project', 'owner': {'login': 'owner'}}
        if endpoint.endswith('/branches/feature%2Ftask'):
            return {'commit': {'sha': SHA}}
        if endpoint.endswith('/branches/main'):
            return {'commit': {'sha': BASE}}
        if endpoint.endswith('/pulls/42'):
            return deepcopy(self.live or self.created)
        if endpoint.endswith('/pulls'):
            if 'POST' in args:
                self.created['body'] = payload['body']
                self.existing = [deepcopy(self.created)]
                if self.failure:
                    raise PublicationError('network unavailable after remote creation')
                return deepcopy(self.created)
            return deepcopy(self.existing)
        raise AssertionError(args)


@pytest.fixture
def local(monkeypatch):
    calls = []
    monkeypatch.setattr(draft, 'require_active_publication_run',
                        lambda **kwargs: calls.append(('owner', kwargs)))
    monkeypatch.setattr(draft, 'require_local_publication_prerequisites',
                        lambda **kwargs: calls.append(('source', kwargs)))
    return calls


class LocalSource:
    def __init__(self, common='/primary/.git'):
        self.common = common

    def run(self, args, **kwargs):
        return SimpleNamespace(stdout='/task' if '--show-toplevel' in args else self.common)


def ensure(remote, value=None):
    return draft.ensure_draft_pr(remote, value or request(), worktree=Path('/task'),
                                 authority_repo=Path('/primary'), command_runner=LocalSource())


def test_create_draft_without_review_findings_or_ready_body(local):
    remote = Remote()
    result = ensure(remote)
    assert result.number == 42 and not result.adopted
    assert result.repository == 'owner/project' and result.head == SHA
    assert result.base_sha == BASE
    assert [payload['draft'] for args, payload in remote.calls if 'POST' in args] == [True]
    assert [name for name, _ in local].count('source') == 2
    assert [name for name, _ in local].count('owner') == 2


def test_adopt_existing_draft_without_mutating_it(local):
    remote = Remote([pr()])
    assert ensure(remote).adopted
    assert not any('POST' in args for args, _ in remote.calls)
    assert all('base=main' not in args for args, _ in remote.calls)


def test_unknown_creation_outcome_is_reconciled_on_next_call(local):
    remote = Remote()
    remote.failure = True
    with pytest.raises(PublicationError, match='unavailable'):
        ensure(remote)
    remote.failure = False
    assert ensure(remote).adopted
    assert sum('POST' in args for args, _ in remote.calls) == 1


@pytest.mark.parametrize('existing', [
    [pr(), pr(number=43)], [pr(state='closed')], [pr(draft=False)],
    [pr(number=True)], [pr(head={'ref': 'feature/task', 'sha': 'c' * 40,
                                'repo': {'full_name': 'owner/project'}})],
    [pr(base={'ref': 'main', 'sha': BASE, 'repo': {'full_name': 'foreign/repo'}})],
    [pr(base={'ref': 'other', 'sha': BASE, 'repo': {'full_name': 'owner/project'}})],
    [pr(html_url='https://github.com/foreign/repo/pull/42')],
])
def test_foreign_ambiguous_closed_ready_or_stale_pr_is_rejected(local, existing):
    remote = Remote(existing)
    with pytest.raises(PublicationError):
        ensure(remote)
    assert not any('POST' in args for args, _ in remote.calls)


@pytest.mark.parametrize('changes', [{'expected_head': 'bad'}, {'expected_base': 'bad'},
    {'expected_repository': 'foreign/repo'}, {'head': 'main'}, {'body': ''}])
def test_invalid_admission_has_no_remote_mutation(local, changes):
    remote = Remote()
    with pytest.raises(PublicationError):
        ensure(remote, request(**changes))
    assert not any('POST' in args for args, _ in remote.calls)


def test_live_pr_drift_after_creation_fails_closed(local):
    remote = Remote()
    remote.live = pr(base={'ref': 'main', 'sha': 'c' * 40,
                          'repo': {'full_name': 'owner/project'}})
    with pytest.raises(PublicationError):
        ensure(remote)


def test_wrong_local_owner_fails_before_remote_calls(monkeypatch, local):
    def reject(**kwargs):
        raise PublicationError('wrong owner')
    monkeypatch.setattr(draft, 'require_active_publication_run', reject)
    remote = Remote()
    with pytest.raises(PublicationError, match='owner'):
        ensure(remote)
    assert not remote.calls


def test_foreign_worktree_authority_rejected_before_remote_calls(local):
    remote = Remote()
    with pytest.raises(PublicationError, match='authority repository'):
        draft.ensure_draft_pr(remote, request(), worktree=Path('/task'),
                              authority_repo=Path('/primary'),
                              command_runner=LocalSource('/foreign/.git'))
    assert not remote.calls


def test_local_source_change_after_creation_is_not_admitted(local, monkeypatch):
    checked = 0
    def changed(**kwargs):
        nonlocal checked
        checked += 1
        if checked == 2:
            raise PublicationError('local HEAD changed')
    monkeypatch.setattr(draft, 'require_local_publication_prerequisites', changed)
    with pytest.raises(PublicationError, match='HEAD changed'):
        ensure(Remote())


def test_foreign_run_pr_is_not_adopted(local):
    remote = Remote([pr(body='<!-- skill-run-id: sr_' + '2' * 32 + ' -->')])
    with pytest.raises(PublicationError, match='run'):
        ensure(remote)
    assert not any('POST' in args for args, _ in remote.calls)


def test_create_body_automatically_binds_current_run(local):
    remote = Remote()
    ensure(remote)
    body = next(payload['body'] for args, payload in remote.calls if 'POST' in args)
    assert body.count('<!-- skill-run-id: ' + request().run_id + ' -->') == 1


def test_request_cannot_hide_a_foreign_run_marker(local):
    remote = Remote()
    with pytest.raises(PublicationError, match='run'):
        ensure(remote, request(body='Objective\n<!-- skill-run-id: sr_' + '2' * 32 + ' -->'))
    assert not remote.calls
