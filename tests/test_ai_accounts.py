"""Behavior checks for the host helper; all credentials/state are synthetic."""
import importlib.machinery
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_ACCOUNTS_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('HOME', str(tmp_path))
    path = Path(__file__).resolve().parents[1] / 'scripts/ai-accounts/ai-accounts'
    loader = importlib.machinery.SourceFileLoader('ai_accounts_test', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    for name in ('a', 'b', 'c'):
        module.write_private(module.STATE / 'claude/tokens' / name, 'synthetic-secret-' + name)
    module.set_active('claude', 'a')
    return module


def test_managed_inherited_credentials_rebind_but_explicit_win(helper):
    inherited = helper.with_accounts('claude', {})
    helper.set_active('claude', 'b')
    assert helper.with_accounts('claude', inherited)['CLAUDE_CODE_OAUTH_TOKEN'] == 'synthetic-secret-b'
    explicit = {'CLAUDE_CODE_OAUTH_TOKEN': 'caller-token'}
    assert helper.with_accounts('claude', explicit) == explicit
    api = {'ANTHROPIC_API_KEY': 'caller-api'}
    assert helper.with_accounts('claude', api) == api


def test_forged_markers_and_explicit_codex_home_preserved(helper):
    base = {'CODEX_HOME': '/explicit', 'AI_ACCOUNTS_CODEX_HOME': '/explicit'}
    assert helper.with_accounts('codex', base) == base
    inherited = helper.with_accounts('claude', {})
    inherited['CLAUDE_CODE_OAUTH_TOKEN'] = 'explicit-replacement'
    assert helper.with_accounts('claude', inherited) == inherited
    for provider, key in [('claude', 'ANTHROPIC_BASE_URL'), ('claude', 'CLAUDE_CODE_USE_BEDROCK'), ('codex', 'OPENAI_BASE_URL')]:
        explicit = {key: 'caller-route'}
        assert helper.with_accounts(provider, explicit) == explicit
        assert helper.launch(provider, 'm', explicit)['outcome'] == 'unmanaged'


def test_exhausted_concurrent_stale_requests_and_terminal(helper, monkeypatch):
    identity = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    calls = []
    def probe(token, model):
        calls.append((token, model))
        return {'ok': token.endswith('-b'), 'http': 200, 'util': {}, 'reset': None}
    monkeypatch.setattr(helper, 'probe_claude_model', probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: helper.exhausted('claude', identity, 'requested-model'), range(2)))
    assert sorted(r['outcome'] for r in results) == ['already-switched', 'switched']
    assert helper.active('claude') == 'b'
    assert all(model == 'requested-model' for _, model in calls)
    current = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    assert helper.exhausted('claude', current, 'requested-model')['outcome'] == 'exhausted'
    assert helper.active('claude') == 'b'


def test_unknown_and_ineligible_never_selected(helper, monkeypatch):
    identity = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    def probe(token, model):
        if token.endswith('-b'):
            raise OSError('offline')
        return {'ok': False, 'http': 200, 'util': {}}
    monkeypatch.setattr(helper, 'probe_claude_model', probe)
    assert helper.exhausted('claude', identity, 'exact-model')['outcome'] == 'unknown'
    assert helper.active('claude') == 'a'


def test_json_boundary_and_unavailable_store(helper, capsys):
    assert helper.main(['exhausted', 'other', '--identity', 'opaque', '--model', 'm']) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'unsupported'
    assert helper.main(['exhausted', 'codex', '--identity', 'opaque', '--model', 'm']) == 0
    output = capsys.readouterr().out
    assert json.loads(output)['outcome'] == 'unmanaged'
    assert 'secret' not in output
    helper.write_private(helper.STATE / 'status.json', '{"checked_at":0,"providers":{}}')
    assert helper.main(['status', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['stale'] is True


def test_weekly_limit_is_quota_but_429_is_not(helper):
    assert helper.USAGE_LIMIT_RE.search('weekly limit')
    assert not helper.USAGE_LIMIT_RE.search('HTTP 429 throttled; try again')


def test_codex_requested_model_and_api_billing(helper, monkeypatch):
    helper.CODEX_MAIN.mkdir()
    helper.write_private(helper.CODEX_MAIN / 'auth.json', json.dumps({'tokens': {'access_token': 'synthetic-codex-a'}}))
    other = helper.STATE / 'codex/homes/b'
    helper.write_private(other / 'auth.json', json.dumps({'tokens': {'access_token': 'synthetic-codex-b'}}))
    helper.set_active('codex', 'main')
    inherited = helper.with_accounts('codex', {})
    helper.set_active('codex', 'b')
    assert helper.with_accounts('codex', inherited)['CODEX_HOME'] == str(other)
    explicit = {'CODEX_HOME': '/caller/home', 'OPENAI_API_KEY': 'caller-key'}
    assert helper.with_accounts('codex', explicit) == explicit
    helper.set_active('codex', 'main')
    monkeypatch.setattr(helper, 'check_codex', lambda name: {'tier': 2, 'models': {'other-model': True}})
    result = helper.exhausted('codex', inherited['AI_ACCOUNTS_CODEX_IDENTITY'], 'exact-model')
    assert result['outcome'] == 'unknown'
    assert helper.active('codex') == 'main'


def test_unrelated_provider_cannot_trigger_replay(helper, tmp_path):
    count = tmp_path / 'runs'
    script = tmp_path / 'fail.py'
    script.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.write_text(p.read_text()+"x" if p.exists() else "x")\nprint("weekly limit",file=sys.stderr)\nsys.exit(4)\n')
    import sys
    code = helper.cmd_run('claude,codex', [sys.executable, str(script), str(count)], {4}, base={}, model='exact-model')
    assert code == 4
    assert count.read_text() == 'x'


def test_run_retries_only_changed_effective_revision(helper, monkeypatch, tmp_path):
    import sys
    script = tmp_path / 'fail.py'
    count = tmp_path / 'runs'
    script.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.write_text(p.read_text()+"x" if p.exists() else "x")\nprint("weekly limit",file=sys.stderr)\nsys.exit(4)\n')
    monkeypatch.setattr(helper, 'probe_claude_model', lambda token, model: {'ok': token.endswith('-b'), 'http': 200, 'util': {}})
    assert helper.cmd_run('claude', [sys.executable, str(script), str(count)], {4}, base={}, model='exact-model', retry_safe=True) == 4
    assert count.read_text() == 'xx'
    count.unlink()
    explicit = {'CLAUDE_CODE_OAUTH_TOKEN': 'explicit'}
    assert helper.cmd_run('claude', [sys.executable, str(script), str(count)], {4}, base=explicit, model='exact-model') == 4
    assert count.read_text() == 'x'


def test_machine_selection_never_prints_credentials(helper, monkeypatch, capsys):
    identity = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    monkeypatch.setattr(helper, 'probe_claude_model', lambda token, model: {'ok': token.endswith('-b'), 'http': 200, 'util': {}})
    assert helper.main(['exhausted', 'claude', '--identity', identity, '--model', 'm']) == 0
    output = capsys.readouterr().out
    assert json.loads(output)['outcome'] == 'switched'
    assert 'synthetic-secret' not in output
    assert 'synthetic-secret' not in (helper.STATE / 'status.json').read_text()


@pytest.mark.parametrize('cache', ['missing', 'expired', 'other-model'])
@pytest.mark.parametrize('usable,known,outcome,active', [(True, True, 'already-switched', 'b'), (False, False, 'unknown', 'b'), (False, True, 'switched', 'c')])
def test_stale_failure_requires_exact_model_evidence(helper, monkeypatch, cache, usable, known, outcome, active):
    identity = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    helper.set_active('claude', 'b')
    if cache != 'missing':
        key = 'claude:m' if cache == 'expired' else 'claude:other'
        helper.write_private(helper.STATE / 'status.json', json.dumps({'selections': {key: {'identity': helper.identity('claude', 'b'), 'until': 0 if cache == 'expired' else helper.now() + 300}}}))
    monkeypatch.setattr(helper, 'model_availability', lambda provider, name, model: (usable, known, .8) if name == 'b' else (True, True, 0))
    assert helper.exhausted('claude', identity, 'm')['outcome'] == outcome
    assert helper.active('claude') == active


def test_launch_then_pin_survives_active_change(helper, monkeypatch, capsys):
    monkeypatch.setattr(helper, 'probe_claude_model', lambda token, model: {'ok': True, 'http': 200, 'util': {}})
    assert helper.main(['launch', 'claude', '--model', 'm']) == 0
    output = capsys.readouterr().out
    launch = json.loads(output)
    assert launch['outcome'] == 'ready'
    assert launch['ready'] is True
    assert 'synthetic-secret' not in output
    monkeypatch.setattr(helper, 'probe_claude_model', lambda *args: pytest.fail('cached launch probed'))
    assert helper.launch('claude', 'm', {})['identity'] == launch['identity']
    helper.set_active('claude', 'b')
    child = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda executable, argv, env: child.append(env))
    monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    helper.main(['exec', 'claude', '--identity', launch['identity'], '--', 'real-harness'])
    assert child[0]['CLAUDE_CODE_OAUTH_TOKEN'] == 'synthetic-secret-a'
    assert child[0]['AI_ACCOUNTS_CLAUDE_IDENTITY'] == launch['identity']


def test_pin_revision_changed_or_explicit_credentials_fails_closed(helper, monkeypatch, capsys):
    revision = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    children = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda *args: children.append(args))
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'caller-explicit')
    assert helper.main(['exec', 'claude', '--identity', revision, '--', 'real-harness']) == 2
    assert json.loads(capsys.readouterr().out)['outcome'] == 'unmanaged'
    monkeypatch.delenv('CLAUDE_CODE_OAUTH_TOKEN')
    helper.write_private(helper.STATE / 'claude/tokens/a', 'synthetic-revised-secret')
    assert helper.main(['exec', 'claude', '--identity', revision, '--', 'real-harness']) == 2
    assert json.loads(capsys.readouterr().out)['outcome'] == 'unmanaged'
    assert children == []


def test_launch_explicit_billing_and_unknown_are_preserved(helper, monkeypatch, capsys):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'explicit-api')
    assert helper.main(['launch', 'claude', '--model', 'm']) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'unmanaged'
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    helper.write_private(helper.STATE / 'status.json', json.dumps({'exhausted': {helper.identity('claude', 'a') + ':m': helper.now() + 300}}))
    monkeypatch.setattr(helper, 'probe_claude_model', lambda *args: {'http': 503, 'ok': False})
    assert helper.main(['launch', 'claude', '--model', 'm']) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'unknown'


def test_unqualified_429_is_unknown(helper, monkeypatch):
    revision = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    monkeypatch.setattr(helper, 'probe_claude_model', lambda *args: {'http': 429, 'ok': False, 'rejected': [], 'util': {}})
    assert helper.exhausted('claude', revision, 'm')['outcome'] == 'unknown'
    assert helper.active('claude') == 'a'


def test_run_default_selects_without_replay(helper, monkeypatch, tmp_path):
    import sys
    count = tmp_path / 'runs'
    script = tmp_path / 'fail.py'
    script.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.write_text(p.read_text()+"x" if p.exists() else "x")\nprint("weekly limit",file=sys.stderr)\nsys.exit(4)\n')
    monkeypatch.setattr(helper, 'probe_claude_model', lambda token, model: {'ok': token.endswith('-b'), 'http': 200, 'util': {}})
    assert helper.cmd_run('claude', [sys.executable, str(script), str(count)], {4}, base={}, model='m') == 4
    assert count.read_text() == 'x'
    assert helper.active('claude') == 'b'


def test_codex_refresh_keeps_managed_binding_and_exhaustion_selection(helper, monkeypatch):
    auth = helper.CODEX_MAIN / 'auth.json'
    principal = {'account_id': 'account-a', 'id_token': 'e30.eyJzdWIiOiJ1c2VyLWEiLCJpc3MiOiJpc3N1ZXIifQ.x'}
    helper.write_private(auth, json.dumps({'tokens': {**principal, 'access_token': 'old'}}))
    helper.write_private(helper.STATE / 'codex/homes/b/auth.json', json.dumps({'tokens': {'account_id': 'account-b', 'access_token': 'other'}}))
    helper.set_active('codex', 'main')
    inherited = helper.with_accounts('codex', {})
    helper.write_private(auth, json.dumps({'tokens': {**principal, 'access_token': 'fresh', 'refresh_token': 'rotated'}}))
    revision = inherited['AI_ACCOUNTS_CODEX_IDENTITY']
    assert helper.managed_name('codex', inherited) == 'main'
    monkeypatch.setattr(helper, 'check_codex', lambda name: {'tier': 2, 'util': 0, 'models': {'m': True}})
    assert helper.exhausted('codex', revision, 'm')['outcome'] == 'switched'
    assert helper.with_accounts('codex', inherited)['CODEX_HOME'].endswith('/b')
    assert helper.exhausted('codex', revision, 'm')['outcome'] == 'already-switched'
    helper.write_private(auth, json.dumps({'tokens': {**principal, 'id_token': 'e30.eyJzdWIiOiJ1c2VyLWIiLCJpc3MiOiJpc3N1ZXIifQ.x'}}))
    assert helper.identity('codex', 'main') != revision
    helper.write_private(auth, json.dumps({'tokens': {'account_id': 'different-account', 'access_token': 'new-login'}}))
    assert helper.identity('codex', 'main') != revision


def test_prepare_keeps_selected_codex_without_claiming_unknown_model_eligibility(helper, monkeypatch):
    helper.write_private(helper.CODEX_MAIN / 'auth.json', '{"tokens":{"account_id":"account-a"}}')
    monkeypatch.setattr(helper, 'check_codex', lambda name: pytest.fail('healthy launch probed'))
    prepared = helper.launch('codex', 'gpt-6.1-sol', {})
    assert prepared['ready'] is True
    assert prepared['identity'] == helper.identity('codex', 'main')


@pytest.mark.parametrize('credential', ['{', '[]', '{"tokens":[1]}', '{"tokens":{"id_token":7}}', '{"tokens":{"id_token":"e30.W10.x"}}', '{"tokens":{"account_id":"shared-workspace"}}'])
def test_partial_or_unidentified_codex_auth_keeps_strict_revision(helper, credential):
    auth = helper.CODEX_MAIN / 'auth.json'
    helper.write_private(auth, credential)
    inherited = helper.with_accounts('codex', {})
    revision = inherited['AI_ACCOUNTS_CODEX_IDENTITY']
    assert helper.managed_name('codex', inherited) == 'main'
    assert helper.launch('codex', 'm', {})['identity'] == revision
    assert helper.exhausted('codex', revision, 'm')['outcome'] == 'exhausted'
    helper.write_private(auth, credential + ' ')
    assert helper.managed_name('codex', inherited) is None
    assert helper.exhausted('codex', revision, 'm')['outcome'] == 'unmanaged'


@pytest.mark.parametrize('used,weekly_reset', [(.4, 2000), (1, 2000), (.4, None)])
def test_probe_retains_each_window_reset(helper, monkeypatch, used, weekly_reset):
    headers = {'anthropic-ratelimit-unified-5h-utilization': str(used),
               'anthropic-ratelimit-unified-7d-utilization': str(used),
               'anthropic-ratelimit-unified-5h-reset': '1000'}
    if weekly_reset is not None:
        headers['anthropic-ratelimit-unified-7d-reset'] = str(weekly_reset)
    monkeypatch.setattr(helper, '_http', lambda req: (200, headers, b''))
    result = helper.probe_claude_model('fake', 'fake')
    assert result['util'] == {'5h': used, '7d': used}
    assert result['resets'] == {'5h': 1000, '7d': weekly_reset}
    assert result['reset'] == (2000 if used == 1 else None)


@pytest.mark.parametrize('window,used,age,cache,owner,allowed', [
    ('7d', .85, 0, 'ok', False, False), ('5h', .85, 0, 'ok', False, False),
    ('7d', .84, 1200, 'ok', False, True), ('5h', .84, 0, 'ok', False, True),
    ('7d', .1, 1201, 'ok', False, False), ('7d', .1, 0, 'missing', False, False),
    ('7d', .1, 0, 'broken', False, False), ('7d', .1, 0, 'account', False, False),
    ('7d', 1, 1201, 'ok', True, True), ('7d', 1, 0, 'missing', True, True)])
@pytest.mark.parametrize('pinned', [False, True])
def test_exec_reserve_policy(helper, monkeypatch, capsys, window, used, age, cache, owner, allowed, pinned):
    monkeypatch.delenv('AI_ACCOUNTS_CLAUDE_OWNER', raising=False)
    if owner:
        monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    monkeypatch.setattr(helper, 'now', lambda: 10000)
    result = {'models': {'fake': {'util': {window: used}, 'resets': {window: 2000}}}}
    status = {'checked_at': helper.now() - age, 'providers': {'claude': {'accounts': {'a': result}}}}
    if cache == 'account':
        status['providers']['claude']['accounts'] = {}
    if cache != 'missing':
        helper.write_private(helper.STATE / 'status.json', '{' if cache == 'broken' else json.dumps(status))
    children = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda *args: children.append(args))
    monkeypatch.setattr(helper, 'probe_claude_model', lambda *args: pytest.fail('exec must not probe'))
    args = ['exec', 'claude']
    if pinned:
        args += ['--identity', helper.identity('claude', 'a')]
    code = helper.main([*args, '--', 'fake-cli'])
    assert code == (127 if allowed else 3)
    assert bool(children) is allowed
    stderr = capsys.readouterr().err
    if not allowed:
        assert 'account a' in stderr
        if used == .85:
            assert window in stderr and '85%' in stderr and helper.iso(2000) in stderr


def test_exec_binds_approved_selection_and_preserves_explicit_auth(helper, monkeypatch):
    monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    original = helper.with_accounts
    def rotate(providers, base, selected=None):
        helper.set_active('claude', 'b')
        return original(providers, base, selected)
    monkeypatch.setattr(helper, 'with_accounts', rotate)
    children = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda command, args, env: children.append(env))
    assert helper.cmd_exec('claude', ['fake']) == 127
    assert children[-1]['CLAUDE_CODE_OAUTH_TOKEN'] == 'synthetic-secret-a'
    assert original('claude', {}, {'claude': None}) == {}
    assert original('claude', {'ANTHROPIC_API_KEY': 'explicit'}, {'claude': 'a'}) == {'ANTHROPIC_API_KEY': 'explicit'}


def test_exec_prefers_account_under_reserve(helper, monkeypatch):
    monkeypatch.delenv('AI_ACCOUNTS_CLAUDE_OWNER', raising=False)
    accounts = {n: {'models': {'m': {'util': {'7d': used}}}} for n, used in (('a', .9), ('b', .5))}
    status = {'checked_at': helper.now(), 'providers': {'claude': {'accounts': accounts}}}
    helper.write_private(helper.STATE / 'status.json', json.dumps(status))
    children = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda command, args, env: children.append(env))
    assert helper.cmd_exec('claude', ['fake']) == 127
    assert children[0]['CLAUDE_CODE_OAUTH_TOKEN'] == 'synthetic-secret-b'
