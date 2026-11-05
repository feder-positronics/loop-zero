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


@pytest.mark.parametrize('provider,key', [('claude', 'CLAUDE_CODE_OAUTH_TOKEN'), ('codex', 'CODEX_HOME')])
def test_managed_credentials_rebind_and_preserve_explicit(helper, provider, key):
    if provider == 'codex':
        for name in ('main', 'b'):
            helper.write_private(helper.codex_home(name) / 'auth.json', '{"tokens":{"access_token":"fake"}}')
    helper.set_active(provider, 'a' if provider == 'claude' else 'main')
    inherited = helper.with_accounts(provider, {})
    helper.set_active(provider, 'b')
    assert helper.with_accounts(provider, inherited)[key] == helper.account_env(provider)[key]
    inherited[key] = 'caller-owned'
    assert helper.with_accounts(provider, inherited) == inherited


@pytest.mark.parametrize('provider,key', [('claude', 'ANTHROPIC_API_KEY'),
    ('claude', 'ANTHROPIC_BASE_URL'), ('claude', 'CLAUDE_CODE_USE_BEDROCK'),
    ('codex', 'OPENAI_API_KEY'), ('codex', 'OPENAI_BASE_URL'), ('codex', 'CODEX_HOME')])
def test_explicit_routes_skip_management(helper, provider, key):
    env = {key: 'caller-owned'}
    assert helper.with_accounts(provider, env) == env
    assert helper.launch(provider, 'm', env)['outcome'] == 'unmanaged'


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
    assert 'synthetic-secret' not in json.dumps(results)
    assert 'synthetic-secret' not in (helper.STATE / 'status.json').read_text()
    assert helper.active('claude') == 'b'
    assert all(model == 'requested-model' for _, model in calls)
    current = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    assert helper.exhausted('claude', current, 'requested-model')['outcome'] == 'exhausted'
    assert helper.active('claude') == 'b'


@pytest.mark.parametrize('http', [None, 429])
def test_failed_or_unqualified_quota_never_selects(helper, monkeypatch, http):
    def probe(token, model):
        if http is None and token.endswith('-b'):
            raise OSError('offline')
        return {'ok': False, 'http': http or 200, 'util': {}}
    monkeypatch.setattr(helper, 'probe_claude_model', probe)
    assert helper.exhausted('claude', helper.identity('claude', 'a'), 'm')['outcome'] == 'unknown'
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


def test_codex_requires_requested_model_evidence(helper, monkeypatch):
    for name in ('main', 'b'):
        helper.write_private(helper.codex_home(name) / 'auth.json', '{"tokens":{"access_token":"fake"}}')
    monkeypatch.setattr(helper, 'check_codex', lambda name: {'tier': 2, 'models': {'other': True}})
    assert helper.exhausted('codex', helper.identity('codex', 'main'), 'exact')['outcome'] == 'unknown'
    assert helper.active('codex') == 'main'


@pytest.mark.parametrize('providers,explicit,retry_safe,runs,active', [
    ('claude,codex', False, False, 1, 'a'), ('claude', False, False, 1, 'b'),
    ('claude', False, True, 2, 'b'), ('claude', True, True, 1, 'a')])
def test_replay_requires_safe_single_provider_and_changed_binding(helper, monkeypatch, tmp_path, providers, explicit, retry_safe, runs, active):
    import sys
    assert helper.USAGE_LIMIT_RE.search('weekly limit')
    assert not helper.USAGE_LIMIT_RE.search('HTTP 429 throttled; try again')
    script, count = tmp_path / 'fail.py', tmp_path / 'runs'
    script.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.write_text(p.read_text()+"x" if p.exists() else "x")\nprint("weekly limit",file=sys.stderr)\nsys.exit(4)\n')
    monkeypatch.setattr(helper, 'probe_claude_model', lambda token, model: {'ok': token.endswith('-b'), 'http': 200, 'util': {}})
    base = {'CLAUDE_CODE_OAUTH_TOKEN': 'explicit'} if explicit else {}
    assert helper.cmd_run(providers, [sys.executable, str(script), str(count)], {4}, base=base, model='m', retry_safe=retry_safe) == 4
    assert count.read_text() == 'x' * runs
    assert helper.active('claude') == active


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


def test_launch_keeps_unknown_model_unknown(helper, monkeypatch):
    helper.write_private(helper.STATE / 'status.json', json.dumps({'exhausted': {helper.identity('claude', 'a') + ':m': helper.now() + 300}}))
    monkeypatch.setattr(helper, 'probe_claude_model', lambda *args: {'http': 503, 'ok': False})
    assert helper.launch('claude', 'm', {})['outcome'] == 'unknown'


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


@pytest.mark.parametrize('window,used,age,cache,owner,allowed', [
    ('7d_oi', .85, 0, 'ok', False, False), ('7d', .85, 0, 'ok', False, False), ('5h', .85, 0, 'ok', False, False),
    ('7d', .84, 1200, 'ok', False, True), ('5h', .84, 0, 'ok', False, True),
    ('7d', .1, 1201, 'ok', False, False), ('7d', .1, 0, 'missing', False, False),
    ('7d', .1, 0, 'broken', False, False), ('7d', .1, 0, 'account', False, False),
    ('7d', 1, 1201, 'ok', True, True), ('7d', 1, 0, 'missing', True, True)])
@pytest.mark.parametrize('schema', ['legacy', 'windows'])
@pytest.mark.parametrize('pinned', [False, True])
def test_exec_reserve_policy(helper, monkeypatch, capsys, window, used, age, cache, owner, allowed, pinned, schema):
    monkeypatch.delenv('AI_ACCOUNTS_CLAUDE_OWNER', raising=False)
    if owner:
        monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    monkeypatch.setattr(helper, 'now', lambda: 10000)
    result = {'models': {'fake': {'util': {window: used}, 'resets': {window: 2000}}}}
    if schema == 'windows':
        result = {'windows': [{'window': window, 'used_pct': used * 100, 'resets_at': 2000}]}
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


@pytest.mark.parametrize('key', ['ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'])
def test_exec_explicit_auth_skips_reserve_guard(helper, monkeypatch, key):
    monkeypatch.delenv('AI_ACCOUNTS_CLAUDE_OWNER', raising=False)
    monkeypatch.setenv(key, 'caller-owned')
    children = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda command, args, env: children.append(env))
    assert helper.cmd_exec('claude', ['fake']) == 127
    assert children[0][key] == 'caller-owned'


@pytest.mark.parametrize('api,session,reverse', [(200, False, False), (403, False, False), (403, True, False), (403, False, True)])
def test_claude_window_report_prefers_api_and_labels_fallback(helper, monkeypatch, capsys, api, session, reverse):
    limits = [{'kind': 'weekly_all', 'percent': 96, 'resets_at': 1793912400},
              {'kind': 'weekly_scoped', 'model': 'fable', 'percent': 17, 'resets_at': 1793998800}]
    headers = {'anthropic-ratelimit-unified-representative-claim': 'seven_day',
        'anthropic-ratelimit-unified-status': 'allowed_warning', 'anthropic-ratelimit-unified-reset': '1793912400',
        'anthropic-ratelimit-unified-7d-utilization': '.96', 'anthropic-ratelimit-unified-7d_oi-utilization': '.17',
        'anthropic-ratelimit-unified-7d_oi-reset': '1793998800', 'anthropic-ratelimit-unified-7d_oi-status': 'allowed'}
    if session:
        headers.update({'anthropic-ratelimit-unified-5h-utilization': '.5', 'anthropic-ratelimit-unified-5h-reset': '1793912400'})
    calls = []
    responses = iter([headers, {**{k: v for k, v in headers.items() if k.endswith('-utilization')}, 'anthropic-ratelimit-unified-7d_oi-status': 'rejected'}][::-1 if reverse else 1])
    def http(req):
        calls.append(req.get_method())
        return (api, {}, json.dumps({'limits': limits}).encode()) if req.get_method() == 'GET' else (200, next(responses), b'')
    monkeypatch.setattr(helper, '_http', http)
    result = helper.check_claude('a')
    helper.print_status({'checked_at': helper.now(), 'providers': {'claude': {'active': 'a', 'accounts': {'a': result}}}})
    output = capsys.readouterr().out
    assert calls == (['GET'] if api == 200 else ['GET', 'POST', 'POST'])
    assert result['next_reset'] == (1793912400 if api == 200 else 1793998800)
    assert 'Fable week: 17% used, resets Fri 06 Nov 22:00 CET' in output
    assert 'all models week: 96% used, resets Thu 05 Nov 22:00 CET — blocks Fable too when full' in output
    assert output.index('Fable week') < output.index('all models week')
    assert ('5-hour' in output) is session
    assert ('fallback' in output and 'HTTP 403' in output) is (api == 403)


@pytest.mark.parametrize('short_reset', [None, 1793912400, 'offline'])
def test_codex_json_and_text_share_each_existing_window(helper, monkeypatch, capsys, short_reset):
    helper.write_private(helper.codex_home('c2') / 'auth.json', '{"tokens":{"access_token":"fake"}}')
    monkeypatch.setattr(helper, 'now', lambda: 10000)
    if short_reset == 'offline':
        monkeypatch.setattr(helper, '_http', lambda req: (_ for _ in ()).throw(OSError('offline')))
        result = helper.check_codex('c2')
        helper.print_status({'checked_at': 10000, 'providers': {'codex': {'active': 'c2', 'accounts': {'c2': result}}}})
        assert result['unknown_reason'] == 'network: offline'
        assert 'unknown (network: offline)' in capsys.readouterr().out
        return
    week = {'used_percent': 96, 'limit_window_seconds': 604800, 'reset_at': 1794384770}
    short = {'used_percent': 99, 'limit_window_seconds': 18000, 'reset_at': short_reset} if short_reset else None
    data = {'rate_limit': {'primary_window': short or week, 'secondary_window': week if short else None}, 'model_usage': {'m': {'available': True}}}
    monkeypatch.setattr(helper, '_http', lambda req: (200, {}, json.dumps(data).encode()))
    result = helper.check_codex('c2')
    assert result['next_reset'] == 1794384770
    status = {'checked_at': 10000, 'providers': {'codex': {'active': 'c2', 'accounts': {'c2': result}}}}
    helper.write_private(helper.STATE / 'status.json', json.dumps(status))
    monkeypatch.setattr(helper, 'now', lambda: 10012)
    helper.main(['status', '--json'])
    windows = json.loads(capsys.readouterr().out)['providers']['codex']['accounts']['c2']['windows']
    week = next(w for w in windows if w['window'] == '7d')
    assert {k: week[k] for k in ('instance', 'used_pct', 'resets_at', 'resets_local',
                               'snapshot_age_s', 'unknown_reason')} == {
        'instance': 'c2', 'used_pct': 96, 'resets_at': '2026-11-11T08:12:50Z',
        'resets_local': 'Wed 11 Nov 09:12 CET', 'snapshot_age_s': 12, 'unknown_reason': None}
    helper.main(['status'])
    output = capsys.readouterr().out
    assert 'week: 96% used, resets Wed 11 Nov 09:12 CET' in output and 'age 12s' in output
    assert ('5-hour' in output) is bool(short)
    assert helper.iso('2026-07-01T12:00:00Z') == 'Wed 01 Jul 14:00 CEST'


@pytest.mark.parametrize('used,reset,forecast', [(90, 1000, 400), (60, 1000, None), (90, 2000, None)])
def test_pace_warns_only_for_increasing_same_reset_samples(helper, monkeypatch, capsys, used, reset, forecast):
    monkeypatch.setattr(helper, 'now', lambda: 300)
    before = {'windows': [{'window': '7d', 'used_pct': 70, 'resets_at': 1000, 'fetched_at': 100}]}
    after = {'windows': [{'window': '7d', 'used_pct': used, 'resets_at': reset, 'fetched_at': 300}]}
    helper.add_pace(after, before)
    assert after['windows'][0].get('runs_out_at') == forecast
    helper.print_status({'checked_at': 300, 'providers': {'claude': {'active': 'a', 'accounts': {'a': after}}}})
    assert ('runs out ~' in capsys.readouterr().out) is bool(forecast)
