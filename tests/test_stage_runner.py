import json
import os
import sys

import pytest

from loopzero import sandbox
from loopzero.cli import _load_report, _save_report

from .conftest import git
from .test_sandbox import bwrap_log, fetched_base, make_config  # noqa: F401


READ_FRAME = 'import json,sys;r=json.load(sys.stdin);r.pop("env");'
STAGE_FIELDS = 'r.update(exit_code=0,duration_s=0,not_started={fallback},tail={tail!r});'
RETURN_FRAME = 'print(json.dumps(r))'
FALLBACK_RUNNER = (sys.executable, '-c', READ_FRAME + STAGE_FIELDS.format(fallback=True, tail='') + RETURN_FRAME)


@pytest.mark.parametrize('base_ref,body,verdict,tails', [
    *[(base_ref, 'r["tail"]=" ".join((env["UV_CACHE_DIR"],env["NODE_ENV"],env["AUDIT_VALUE"],os.environ["AUDIT_VALUE"]));print(json.dumps(r))',
       True, ['/operator/cache production [REDACTED] [REDACTED]'] * 2) for base_ref in ('stale', 'missing', 'recorded', 'abs-link', 'path-link')],
    ('stale', 'r["exit_code"]=7;r["tail"]="ok "+env["AUDIT_VALUE"];print(json.dumps(r));sys.exit(75)', False, ['ok [REDACTED]']),
    ('stale', 'print("x" * 3000 + "worker socket unavailable " + env["AUDIT_VALUE"], file=sys.stderr);sys.exit(42)', False, None),
    ('stale', 'r["exit_code"]=7;r["tail"]=env["AUDIT_VALUE"]+"Ω"*8183+" newest";print(json.dumps(r))', False,
     ['[REDACTED]' + 'Ω' * 8183 + ' newest']),
    ('stale', 'r["exit_code"]=7;r["tail"]="Ω"*10000+" newest";print(json.dumps(r))', False,
     ['Ω' * 8188 + ' newest']),
    ('stale', 'subprocess.run(["git","-C",repo,"-c","user.name=test","-c","user.email=t@e","commit","--allow-empty","-m","drift"],stdout=sys.stderr,check=True);print(json.dumps(r))', False, None),
    ('stale', 'r.update(not_started=True,duration_s=0,tail="");print(json.dumps(r))', True, ['one', 'two']),
    *[('stale', body, False, None) for body in [
        '', '''print('{"x":' * 100000 + "0" + "}" * 100000)''', 'print("{")', 'print(json.dumps(r));print(json.dumps(r))',
        'del r["exit_code"];print(json.dumps(r))', 'del r["worktree"];print(json.dumps(r))',
        'r["duration_s"]=float("nan");print(json.dumps(r))',
        'r["exit_code"]=True;print(json.dumps(r))',
        'r["network"]=1;print(json.dumps(r))', 'r["network"]=False;print(json.dumps(r))',
        'print(json.dumps(r));sys.exit(1)', 'r["not_started"]=True;print(json.dumps(r))',
        *[f'r["{field}"]="wrong";print(json.dumps(r))' for field in ('head', 'base', 'command', 'env_sha256', 'network', 'worktree')],
    ]],
])
def test_runner(git_repo, tmp_path, bwrap_log, monkeypatch, body, verdict, tails, base_ref):  # noqa: F811
    git(git_repo, 'switch', '-c', 'task')
    git(git_repo, 'commit', '--allow-empty', '-m', 'advance')
    base = git(git_repo, 'rev-parse', 'HEAD').strip()
    if base_ref == 'recorded':
        (git_repo / '.git/info/exclude').write_text('.loopzero/\n')
        (git_repo / '.loopzero').mkdir()
        (git_repo / '.loopzero/task.md').write_text(f'Base: {base}\n')
    else:
        git(git_repo, 'update-ref', 'refs/remotes/origin/main', base)
    if base_ref == 'missing':
        git(git_repo, 'branch', '-D', 'main')
    monkeypatch.setenv("AUDIT_VALUE", "opaque-fake-wrapper-password")
    configured = {"UV_CACHE_DIR": "/operator/cache", "NODE_ENV": "production", "AUDIT_VALUE": "opaque-fake-db-password"}
    expected = dict(sandbox.SANDBOX_ENV, **configured)
    body = (f'repo=r["worktree"]\nassert r["base"] == "{base}"\n'
            'assert not Path.cwd().is_relative_to(Path(repo))\n'
            f'env=r.pop("env");assert env == {expected!r} and r["network"] is True\n'
            f'assert r["env_sha256"] == hashlib.sha256(json.dumps({expected!r},'
            'sort_keys=True,separators=(",", ":")).encode()).hexdigest()\n' + body)
    path = tmp_path / 'runner.py'
    preamble = 'import hashlib,json,os,sys,subprocess\nfrom pathlib import Path\nr=json.load(sys.stdin)\n'
    launcher = (sys.executable, str(path))
    if base_ref in ('abs-link', 'path-link'):
        link = tmp_path / 'runner-python'
        link.symlink_to(sys.executable)
        monkeypatch.setenv('PATH', f'{tmp_path}:' + os.environ['PATH'])
        preamble += f'assert sys.executable == {str(link)!r}\n'
        launcher = (link.name if base_ref == 'path-link' else str(link), str(path), 'relative/argument')
    path.write_text(preamble + STAGE_FIELDS.format(fallback=False, tail='ok') + '\n' + body)
    calls = []
    config = make_config(checks=('echo one', 'echo two'), check_runner=launcher, network=True,
                         env=tuple(configured.items()), env_allowlist=("PATH", "AUDIT_VALUE"))
    if tails is None:
        with pytest.raises(sandbox.StageRunnerFailure) as raised:
            sandbox.run_checks(config, git_repo, progress=calls.append)
        assert calls == ['echo one']
        error = raised.value
        assert 'runner failure, not a check result' in str(error)
        if 'sys.exit(42)' in body:
            assert error.exit_code == 42
            assert str(error).endswith(('x' * 3000 + 'worker socket unavailable [REDACTED]')[-2000:])
        if '* 100000' in body:
            assert any(word in str(error).lower() for word in ('recursion', 'stack overflow'))
        if 'drift' in body:
            assert 'Worktree changed after stage' in str(error)
        assert not (git_repo / '.loopzero/checks.json').exists()
        return
    report = sandbox.run_checks(config, git_repo, progress=calls.append)
    assert report.ok is verdict
    if 'exit_code"]=7' in body:
        assert report.results[0].exit_code == 7
    assert [r.tail for r in report.results] == tails
    if not verdict:
        assert calls == ['echo one']
        assert sum(r.tail == 'ok' for r in report.results) <= 1
    current_head = _save_report(git_repo, report)
    assert current_head is None and _load_report(git_repo) == report
    receipt = json.loads((git_repo / '.loopzero/checks.json').read_text())
    assert receipt.keys() == {'head', 'dirty', 'results', 'settings_sha256'}


@pytest.mark.parametrize('dirty', [True, False])
def test_runner_unavailable(git_repo, monkeypatch, dirty, fetched_base):  # noqa: F811
    if dirty:
        (git_repo / 'dirty').write_text('x')
    monkeypatch.setattr(sandbox.shutil, 'which', lambda name: None)
    cfg = make_config(check_runner=FALLBACK_RUNNER)
    if not dirty:
        with pytest.raises(sandbox.StageRunnerFailure, match='bwrap binary missing'):
            sandbox.run_checks(cfg, git_repo)
    else:
        report = sandbox.run_checks(cfg, git_repo)
        assert not report.ok and report.dirty
        assert 'bwrap binary missing' not in report.results[0].tail


@pytest.mark.parametrize('kind', ['executable', 'argument', 'symlink', 'path', 'executable_symlink', 'option', 'option_symlink', 'short', 'list', 'parent', 'colon_link', 'colon_tree'])
def test_runner_rejects_task_paths(git_repo, tmp_path, monkeypatch, kind):
    runner = git_repo / 'runner'
    runner.write_text('#!/bin/sh\nexit 0\n')
    runner.chmod(0o755)
    git(git_repo, 'add', 'runner')
    git(git_repo, 'commit', '-m', 'runner')
    git(git_repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    link = tmp_path / ('runner:link' if kind == 'colon_link' else 'runner-link')
    link.symlink_to(runner)
    if kind == 'colon_tree':
        git(git_repo, 'worktree', 'add', '-q', '--detach', str(tmp_path / 'task:tree'))
        git_repo, runner = tmp_path / 'task:tree', tmp_path / 'task:tree' / 'runner'
    monkeypatch.setenv('PATH', f'{git_repo}:' + os.environ['PATH'])
    prefix = {'executable': (str(runner),), 'argument': (sys.executable, str(runner)),
              'symlink': (sys.executable, str(link)), 'path': ('runner',),
              'executable_symlink': (str(link),), 'option': (sys.executable, f'--script={runner}'),
              'option_symlink': (sys.executable, f'--script={link}'),
              'short': (sys.executable, f'-X{runner}'),
              'list': (sys.executable, f'/operator/safe:{runner}'),
              'parent': (sys.executable, '../escape'), 'colon_link': (str(link),),
              'colon_tree': (str(runner),)}[kind]
    with pytest.raises(sandbox.StageRunnerFailure, match='references task worktree'):
        sandbox.run_checks(make_config(check_runner=prefix), git_repo)


def test_runner_timeout(git_repo, monkeypatch, fetched_base):  # noqa: F811
    original = sandbox._proc.run
    def run(argv, **kwargs):
        if argv[0] == sys.executable:
            raise sandbox._proc.ProcTimeout('deadline', '{"exit_code":',
                                            stderr='partial opaque-fake-db-password', duration_s=0.25)
        return original(argv, **kwargs)
    monkeypatch.setattr(sandbox._proc, 'run', run)
    cfg = make_config(check_runner=(sys.executable,), env=(("AUDIT_VALUE", "opaque-fake-db-password"),))
    with pytest.raises(sandbox.StageRunnerFailure) as raised:
        sandbox.run_checks(cfg, git_repo, timeout=0.1)
    assert raised.value.exit_code == 124
    assert 'partial [REDACTED]' in str(raised.value) and 'timed out after 0.1s' in str(raised.value)


@pytest.mark.parametrize('change', ['fallback', 'base'])
def test_runner_reuse_binds_execution_context(git_repo, bwrap_log, fetched_base, change):  # noqa: F811
    from dataclasses import replace

    (git_repo / '.git/info/exclude').write_text('.loopzero/\n')
    command = 'echo stage-ran; exit 7' if change == 'fallback' else 'echo one'
    cfg = make_config(checks=(command,), inputs={command: ('README.md',)})
    runner = FALLBACK_RUNNER
    if change == 'fallback':
        passing = sandbox.run_checks(replace(cfg, checks=('echo seed',)), git_repo)
        result = replace(passing.results[0], command=command,
                         input_key=sandbox._input_key(cfg, git_repo, command, fetched_base))
        _save_report(git_repo, replace(passing, results=(result,)))
    else:
        cfg = replace(cfg, check_runner=runner)
        _save_report(git_repo, sandbox.run_checks(cfg, git_repo))
        git(git_repo, 'commit', '--allow-empty', '-m', 'advance base')
        git(git_repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    cfg = replace(cfg, check_runner=runner)
    report = sandbox.run_checks(cfg, git_repo)
    assert report.results[0].reused_from is None
    assert report.results[0].exit_code == (7 if change == 'fallback' else 0)
    _save_report(git_repo, report)
    again = sandbox.run_checks(cfg, git_repo)
    assert again.results[0].exit_code == report.results[0].exit_code
    assert bool(again.results[0].reused_from) is (change == 'base')
