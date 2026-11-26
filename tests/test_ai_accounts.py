"""Behavior checks for the host helper; all credentials/state are synthetic."""
import importlib.machinery
import importlib.util
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, call

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


@pytest.fixture
def exec_envs(helper, monkeypatch):
    children = []
    monkeypatch.setattr(helper.os, 'execvpe', lambda command, args, env: children.append(env))
    return children


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


@pytest.mark.parametrize('http', [None, 429, 503])
def test_failed_or_unqualified_quota_never_selects(helper, monkeypatch, http):
    def probe(token, model):
        if http is None and token.endswith('-b'):
            raise OSError('offline')
        return {'ok': False, 'http': http or 200, 'util': {}}
    monkeypatch.setattr(helper, 'probe_claude_model', probe)
    identity = helper.identity('claude', 'a')
    if http == 503:
        helper.write_private(helper.STATE / 'status.json', json.dumps({'exhausted': {identity + ':m': helper.now() + 300}}))
        result = helper.launch('claude', 'm', {})
    else:
        result = helper.exhausted('claude', identity, 'm')
    assert result['outcome'] == 'unknown' and helper.active('claude') == 'a'


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


def test_run_returns_after_leader_exits_with_inherited_stderr(helper):
    argv = [helper.sys.executable, helper.__file__, 'run', 'claude', '--', helper.sys.executable,
            '-c', 'import subprocess, sys; subprocess.Popen(["sleep", "6"]); sys.exit(7)']
    assert helper.subprocess.run(argv, timeout=5).returncode == 7


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


def test_launch_then_pin_survives_active_change(helper, monkeypatch, capsys, exec_envs):
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
    monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    helper.main(['exec', 'claude', '--identity', launch['identity'], '--', 'real-harness'])
    assert exec_envs[0]['CLAUDE_CODE_OAUTH_TOKEN'] == 'synthetic-secret-a'
    assert exec_envs[0]['AI_ACCOUNTS_CLAUDE_IDENTITY'] == launch['identity']


@pytest.mark.parametrize('explicit', [False, True], ids=['changed-revision', 'caller-credential'])
def test_pin_revision_changed_or_explicit_credentials_fails_closed(helper, monkeypatch, capsys, exec_envs, explicit):
    revision = helper.with_accounts('claude', {})['AI_ACCOUNTS_CLAUDE_IDENTITY']
    if explicit:
        monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'caller-explicit')
    else:
        helper.write_private(helper.STATE / 'claude/tokens/a', 'synthetic-revised-secret')
    assert helper.main(['exec', 'claude', '--identity', revision, '--', 'real-harness']) == 2
    assert json.loads(capsys.readouterr().out)['outcome'] == 'unmanaged'
    assert exec_envs == []


def test_codex_monitor_keeps_usable_current_until_spent(helper):
    for tier, usage, chosen in [(2, .87, 'c3'), (1, .949, 'c3'), (0, .95, 'c2')]:
        accounts = {'c3': {'tier': tier, 'util': usage}, 'c2': {'tier': 2, 'util': .28}}
        assert helper.select('codex', accounts, 'c3') == chosen


def test_codex_refresh_keeps_managed_binding_and_exhaustion_selection(helper, monkeypatch):
    auth = helper.CODEX_MAIN / 'auth.json'
    principal = {'account_id': 'account-a', 'id_token': 'e30.eyJzdWIiOiJ1c2VyLWEiLCJpc3MiOiJpc3N1ZXIifQ.x'}
    helper.write_private(auth, json.dumps({'tokens': {**principal, 'access_token': 'old' * 100, 'refresh_token': 'old-refresh'}}))
    helper.write_private(helper.STATE / 'codex/homes/b/auth.json', json.dumps({'tokens': {'account_id': 'account-b', 'access_token': 'other'}}))
    helper.set_active('codex', 'main')
    inherited = helper.with_accounts('codex', {})
    refreshed = {"access_token": "fresh", "refresh_token": "rotated"}
    monkeypatch.setattr(helper, "_http", lambda _: (200, {}, json.dumps(refreshed)))
    write = helper.os.pwrite

    def interrupt(fd, data, offset):
        write(fd, data, offset)
        raise KeyboardInterrupt

    with auth.open("rb") as pinned, monkeypatch.context() as patch:
        patch.setattr(helper.os, "pwrite", interrupt)
        patch.setattr(helper, "jwt_claims", lambda _: {"client_id": "test"})
        with pytest.raises(KeyboardInterrupt):
            helper.refresh_codex(auth, json.loads(auth.read_text()))
        assert json.load(pinned)["tokens"] == {**principal, **refreshed}
        assert auth.stat().st_ino == helper.os.fstat(pinned.fileno()).st_ino
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
    ('7d', .1, 0, 'broken', False, False), ('7d', .1, 0, 'account', False, False), ('7d', .1, 0, 'no-windows', False, False),
    ('7d', 1, 1201, 'ok', True, True), ('7d', 1, 0, 'missing', True, True)])
@pytest.mark.parametrize('pinned', [False, True])
def test_exec_reserve_policy(helper, monkeypatch, capsys, exec_envs, window, used, age, cache, owner, allowed, pinned):
    monkeypatch.delenv('AI_ACCOUNTS_CLAUDE_OWNER', raising=False)
    if owner:
        monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    monkeypatch.setattr(helper, 'now', lambda: 10000)
    result = {'windows': [{'window': window, 'used_pct': used * 100, 'resets_at': 2000}]}
    status = {'checked_at': helper.now() - age, 'providers': {'claude': {'accounts': {'a': result}}}}
    if cache == 'no-windows':
        result.pop('windows')
    if cache == 'account':
        status['providers']['claude']['accounts'] = {}
    if cache != 'missing':
        helper.write_private(helper.STATE / 'status.json', '{' if cache == 'broken' else json.dumps(status))
    monkeypatch.setattr(helper, 'probe_claude_model', lambda *args: pytest.fail('exec must not probe'))
    args = ['exec', 'claude']
    if pinned:
        args += ['--identity', helper.identity('claude', 'a')]
    code = helper.main([*args, '--', 'fake-cli'])
    assert code == (127 if allowed else 3)
    assert bool(exec_envs) is allowed
    stderr = capsys.readouterr().err
    if not allowed:
        assert 'account a' in stderr
        if used == .85:
            assert window in stderr and '85%' in stderr and helper.iso(2000) in stderr


def test_exec_binds_approved_selection_and_preserves_explicit_auth(helper, monkeypatch, exec_envs):
    monkeypatch.setenv('AI_ACCOUNTS_CLAUDE_OWNER', '1')
    original = helper.with_accounts
    def rotate(providers, base, selected=None):
        helper.set_active('claude', 'b')
        return original(providers, base, selected)
    monkeypatch.setattr(helper, 'with_accounts', rotate)
    assert helper.cmd_exec('claude', ['fake']) == 127
    assert exec_envs[-1]['CLAUDE_CODE_OAUTH_TOKEN'] == 'synthetic-secret-a'
    assert original('claude', {}, {'claude': None}) == {}
    assert original('claude', {'ANTHROPIC_API_KEY': 'explicit'}, {'claude': 'a'}) == {'ANTHROPIC_API_KEY': 'explicit'}


@pytest.mark.parametrize('key', [None, 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'])
def test_exec_selects_under_reserve_or_preserves_caller_auth(helper, monkeypatch, exec_envs, key):
    monkeypatch.delenv('AI_ACCOUNTS_CLAUDE_OWNER', raising=False)
    if key is None:
        accounts = {n: {'windows': [{'window': '7d', 'used_pct': used}]} for n, used in (('a', 90), ('b', 50))}
        status = {'checked_at': helper.now(), 'providers': {'claude': {'accounts': accounts}}}
        helper.write_private(helper.STATE / 'status.json', json.dumps(status))
        key, expected = 'CLAUDE_CODE_OAUTH_TOKEN', 'synthetic-secret-b'
    else:
        expected = 'caller-owned'
        monkeypatch.setenv(key, expected)
    assert helper.cmd_exec('claude', ['fake']) == 127
    assert exec_envs[0][key] == expected


@pytest.mark.parametrize('code,retry_after,delay,previous', [
    (429, '3600', 3600, None), (429, 'Thu, 01 Jan 2026 01:00:00 GMT', 3600, None),
    (429, '9' * 400, 86400, None), (429, '1000000000000', 86400, None),
    (429, 'Fri, 31 Dec 9999 23:59:59 GMT', 86400, None),
    (429, 'Thu, 01 Jan 2026 01:00:00', 3600, None),
    (429, None, 1800, None), (429, 'invalid', 1800, None), (429, '120', 1800, None),
    (200, None, 1800, None), (503, None, 1800, None), (None, None, 1800, None),
    *((200, None, 1800, deadline) for deadline in ('inf', '-inf', 'nan', '1e308', '1767312001')),
])
def test_claude_usage_backoff_persists_and_uses_headers(helper, monkeypatch, code, retry_after, delay, previous):
    start = 1767225600  # 2026-01-01 00:00:00 UTC
    clock, calls = [start], []
    deadline = helper.STATE / 'claude/usage-next/a'
    if previous is not None:
        helper.write_private(deadline, previous)
    limits = [{'kind': 'weekly_all', 'percent': 10, 'resets_at': start + 86400}]
    headers = {'anthropic-ratelimit-unified-7d-utilization': '.25'}

    def http(req):
        calls.append((req.get_method(), req.get_header('Authorization')))
        if req.get_method() == 'POST':
            return 200, headers, b''
        if code is None:
            raise helper.urllib.error.URLError('synthetic network failure')
        return code, {'retry-after': retry_after} if retry_after else {}, json.dumps({'limits': limits}).encode()

    monkeypatch.setattr(helper, 'now', lambda: clock[0])
    monkeypatch.setattr(helper, '_http', http)
    first = helper.check_claude('a')
    assert first['source'] == ('OAuth usage API' if code == 200 else 'headers (fallback)')
    assert float(deadline.read_text()) == start + delay
    assert deadline.stat().st_mode & 0o777 == 0o600
    if code == 200:
        assert calls == [('GET', 'Bearer synthetic-secret-a')]
    log_path = helper.STATE / 'log'
    logged = log_path.read_text() if log_path.exists() else ''
    if code not in (200, None):
        assert len(logged.splitlines()) == 1
        assert f'claude a: usage API HTTP {code}; backoff until ' in logged
        assert f'backoff until {helper.iso(start + delay)}' in logged and 'CET' in logged
    else:
        assert logged == ''
    assert 'synthetic-secret' not in logged

    # Reload as the next timer process: only persisted state can suppress its GET.
    fresh = importlib.util.module_from_spec(helper.__spec__)
    helper.__loader__.exec_module(fresh)
    monkeypatch.setattr(fresh, 'now', lambda: clock[0])
    monkeypatch.setattr(fresh, '_http', http)
    calls.clear()
    clock[0] = start + delay - 1
    skipped = fresh.check_claude('a')
    assert [method for method, _ in calls] == ['POST', 'POST']
    assert skipped['source'] == 'headers (fallback)'
    assert skipped['source_reason'] == f'next usage API read at {helper.iso(start + delay)}'
    assert skipped['util'] == .25
    assert skipped['windows'][0]['used_pct'] == 25
    assert (log_path.read_text() if log_path.exists() else '') == logged

    # Another token has its own deadline; the first token resumes at the boundary.
    calls.clear()
    fresh.check_claude('b')
    assert calls[0] == ('GET', 'Bearer synthetic-secret-b')
    calls.clear()
    clock[0] = start + delay
    fresh.check_claude('a')
    assert calls[0] == ('GET', 'Bearer synthetic-secret-a')


@pytest.mark.parametrize('model_field', [{'scope': {'model': {'display_name': 'Fable', 'id': 'other'}}}, {'model': {'id': 'fable'}}, {'model': 'fable'}])
@pytest.mark.parametrize('api,session,reverse', [(200, False, False), (403, False, False), (403, True, False), (403, False, True)])
def test_claude_window_report_prefers_api_and_labels_fallback(helper, monkeypatch, capsys, api, session, reverse, model_field):
    limits = [{'kind': 'weekly_all', 'percent': 96, 'resets_at': 1793912400}, {'kind': 'weekly_scoped', **model_field, 'percent': 17, 'resets_at': '2026-11-06T21:00:00Z'}]
    headers = {f'anthropic-ratelimit-unified-{key}': value for key, value in { 'representative-claim': 'seven_day', 'status': 'allowed_warning', 'reset': '1793912400', '7d-utilization': '.96', '7d_oi-utilization': '.17', '7d_oi-reset': '1793998800', '7d_oi-status': 'allowed', **({'5h-utilization': '.5', '5h-reset': '1793912400'} if session else {})}.items()}
    calls, responses = [], iter([headers, {**{k: v for k, v in headers.items() if k.endswith('-utilization')}, 'anthropic-ratelimit-unified-7d_oi-status': 'rejected'}][::-1 if reverse else 1])
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


@pytest.mark.parametrize('short_reset', [None, 1793912400, 'offline', 'all-unknown'])
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
    short = {'used_percent': 99, 'limit_window_seconds': 18000, 'reset_at': short_reset} if isinstance(short_reset, int) else None
    data = {'rate_limit': {'primary_window': short or week, 'secondary_window': week if short else None}, 'model_usage': {'m': {'available': True}, 'partial': {}}}
    if short_reset == 'all-unknown':
        del data['model_usage']['m']
    monkeypatch.setattr(helper, '_http', lambda req: (200, {}, json.dumps(data).encode()))
    result = helper.check_codex('c2')
    assert result['models_unknown'] == ['partial']
    if short_reset == 'all-unknown':
        assert result['tier'] == -1 and result['error'] == 'usage response has no known model availability'
        return
    assert result['models'] == {'m': True}
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


@pytest.mark.parametrize('name', ['loopzero', 'codex', 'claude'])
@pytest.mark.parametrize('pin_state', ['absent', 'missing', 'nonexecutable', 'executable', 'symlink'])
def test_shim_prefers_verified_codex_only_for_loopzero(helper, tmp_path, monkeypatch, fake_bin, fake_tool,
                                                     name, pin_state):
    shims = tmp_path / 'shims'
    shims.mkdir()
    for shim in helper.SHIMS:
        (shims / shim).symlink_to(Path(helper.__file__))
        fake_tool(shim, 'exit 0\n')
    pin = tmp_path / '.local/lib/codex-reviewer'
    if pin_state != 'absent':
        pin_home = pin.with_name('codex-reviewer-test') if pin_state == 'symlink' else pin
        pin_home.mkdir(parents=True)
        if pin_home != pin:
            pin.symlink_to(pin_home, target_is_directory=True)
        if pin_state != 'missing':
            codex = pin / 'codex'
            codex.write_text('#!/bin/sh\nexit 0\n')
            codex.chmod(0o755 if pin_state != 'nonexecutable' else 0o644)
    codex_home = tmp_path / 'codex-home'
    arg0 = codex_home / 'tmp/arg0-linux/codex-arg0ABC123'
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    monkeypatch.setenv('PATH', f'{shims}:{arg0}:{fake_bin}')
    monkeypatch.setattr(helper.sys, 'argv', [str(shims / name)])
    execute = Mock()
    monkeypatch.setattr(helper.os, 'execve', execute)
    helper.cmd_shim(name, ['--help'])
    execute.assert_called_once()
    real, argv, env = execute.call_args.args
    assert real == str(fake_bin / name)
    assert argv == [real, '--help']
    expected = [shims, arg0, fake_bin]
    if name == 'loopzero':
        expected = [pin, fake_bin] if pin_state in ('executable', 'symlink') else [fake_bin]
    assert env['PATH'].split(helper.os.pathsep) == list(map(str, expected))


@pytest.mark.parametrize('provider,email,shown', [
    ('claude', 'a@example.com', ' *a (Max)  a@example.com  '),
    ('claude', None, ' *a (Max)  email unknown (ai-accounts label claude a <email>)  '),
    ('codex', None, ' *a (Max)  email unknown  ')])
def test_status_prints_account_email_after_plan(helper, capsys, provider, email, shown):
    result = {'tier': 0, 'plan': 'max', 'email': email, 'windows': [], 'unknown_reason': 'no data'}
    helper.print_status({'checked_at': helper.now(), 'providers': {provider: {'active': 'a', 'accounts': {'a': result}}}})
    assert shown in capsys.readouterr().out


def test_account_lock_timeout_leaves_files_untouched(helper, monkeypatch):
    for name in ("main", "b", "c"):
        auth = {"tokens": {"account_id": name, "access_token": "expiring"}}
        helper.write_private(helper.codex_home(name) / "auth.json", json.dumps(auth))
    helper.set_active("codex", "main")
    status = helper.STATE / "status.json"
    cooldown = {"exhausted": {helper.identity("codex", "main") + ":model": helper.now() + 300}}
    helper.write_private(status, json.dumps(cooldown))
    for key in (*helper.EXPLICIT_ROUTES["codex"], "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    elapsed = 0.0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def premature_probe(*args):
        pytest.fail("account probing started before the home lock was acquired")

    monkeypatch.setattr(helper.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(helper.time, "sleep", advance)
    monkeypatch.setattr(helper, "check_claude", premature_probe)
    monkeypatch.setattr(helper, "codex_in_use", lambda _: False)
    monkeypatch.setattr(helper, "refresh_codex", premature_probe)
    monkeypatch.setattr(helper, "_http", premature_probe)
    check = partial(helper.check, quiet=True)
    for name, operations in (
        (".lock", (check,)),
        (".codex-sync.lock", (
            check,
            lambda: helper.launch("codex", "model", {}),
            lambda: helper.cmd_add("codex", "new"),
            lambda: helper.cmd_pinned_exec("codex", helper.identity("codex", "b"), ["unused"]),
            lambda: helper.exhausted("codex", helper.identity("codex", "main"), "model", retry_env={}),
        )),
    ):
        with (helper.STATE / name).open("a") as holder:
            helper.fcntl.flock(holder, helper.fcntl.LOCK_EX)
            for operation in operations:
                with pytest.raises(helper.AccountLockTimeout, match=rf"^{re.escape(name)} is busy"):
                    operation()
    assert elapsed == 360
    assert not helper.codex_home("new").exists()
    assert helper.active("codex") == "main"
    assert helper.active("claude") == "a"
    assert json.loads(status.read_text()) == cooldown

    acquire = Mock(wraps=helper._locked)
    monkeypatch.setattr(helper, "_locked", acquire)
    monkeypatch.setattr(helper, "check_claude", lambda _: {"tier": -1})
    monkeypatch.setattr(helper, "check_codex", lambda _: {"tier": -1})
    helper.check(quiet=True)
    assert acquire.call_args_list.count(call(".codex-sync.lock")) == 1
    helper.set_active("codex", "b")
    assert helper.with_accounts("codex,codex", {})["CODEX_HOME"] == str(helper.codex_home("b"))
    assert acquire.call_args_list.count(call(".codex-sync.lock")) == 2


def test_provider_deadline_covers_http_error_body(helper, monkeypatch):
    request = helper.urllib.request.Request("https://example.invalid")
    body = BytesIO()
    monkeypatch.setattr(body, "read", lambda: helper.signal.raise_signal(helper.signal.SIGALRM))

    def open_(*args, **kwargs):
        raise helper.urllib.error.HTTPError(request.full_url, 503, "Busy", {}, body)

    monkeypatch.setattr(helper.urllib.request, "urlopen", open_)
    with pytest.raises(TimeoutError, match="provider HTTP request timed out"):
        helper._http(request, timeout=1)


@pytest.mark.parametrize("symlink", [False, True])
def test_account_staging_preserves_colliding_path(helper, tmp_path, symlink):
    path = helper.STATE / "status.json"
    staged = path.with_name(f"{path.name}.{helper.os.getpid()}.{helper.threading.get_ident()}.tmp")
    target = tmp_path / "unrelated" if symlink else staged
    target.write_text("preserve")
    if symlink:
        staged.symlink_to(target)
    helper.write_private(path, "{}")
    assert path.read_text() == "{}" and target.read_text() == "preserve"


def test_account_lock_preserves_existing_file_and_rejects_symlink(helper):
    lock = helper.STATE / ".lock"
    lock.write_text("preserve")
    with helper._locked(lock.name):
        assert lock.read_text() == "preserve"
    (helper.STATE / ".codex-sync.lock").symlink_to(lock)
    with pytest.raises(OSError), helper._locked(".codex-sync.lock"):
        pytest.fail("redirected lock opened")
    assert lock.read_text() == "preserve"


@pytest.mark.parametrize("outcome", ["published", "copy failed"])
def test_account_env_moves_shared_entry_across_devices(helper, monkeypatch, outcome):
    if not Path("/dev/shm").is_dir() or Path("/dev/shm").stat().st_dev == helper.STATE.stat().st_dev:
        pytest.skip("requires two filesystems")
    entry = helper.codex_home("extra") / "session-index.jsonl"
    helper.write_private(entry, "original")
    helper.write_private(entry.with_name("auth.json"), "{}")
    copy = helper.shutil.copy2
    with tempfile.TemporaryDirectory(dir="/dev/shm") as main:
        monkeypatch.setattr(helper, "CODEX_MAIN", Path(main))
        def stage(src, dst, **kwargs):
            copy(src, dst, **kwargs)
            if outcome == "copy failed":
                raise OSError("copy interrupted")
        monkeypatch.setattr(helper.shutil, "copy2", stage)
        if outcome == "copy failed":
            with pytest.raises(OSError, match="copy interrupted"):
                helper.account_env("codex", "extra")
        else:
            helper.account_env("codex", "extra")
        assert entry.read_text() == "original" and entry.is_symlink() == (outcome == "published")
        assert list(Path(main).iterdir()) == ([] if outcome == "copy failed" else [Path(main) / entry.name])


@pytest.mark.parametrize("unsupported", [None, "ENOSYS", "EINVAL"])
def test_sync_preserves_a_shared_directory_created_during_promotion(helper, monkeypatch, unsupported):
    home = helper.codex_home("extra")
    home.mkdir(parents=True)
    helper.CODEX_MAIN.mkdir()
    source = home / "sessions"
    source.mkdir()
    (source / "old").write_text("keep local")
    (home / "notes").write_text("promote")
    rename = helper._rename_noreplace

    def create_destination(entry, target):
        if entry == source:
            target.mkdir()
            if unsupported:
                raise OSError(getattr(helper.errno, unsupported), "no-replace unsupported")
        rename(entry, target)

    monkeypatch.setattr(helper, "_rename_noreplace", create_destination)
    helper.sync_codex_home(home)
    assert (source / "old").read_text() == "keep local"
    assert list((helper.CODEX_MAIN / "sessions").iterdir()) == []
    assert (home / "notes").is_symlink() and (home / "notes").read_text() == "promote"
