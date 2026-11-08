import json
import os
import sys

import pytest

from loopzero import sandbox
from loopzero.cli import _load_report, _save_report
from loopzero.config import ConfigError, load

from .conftest import git
from .test_sandbox import bwrap_log, make_config  # noqa: F401


@pytest.mark.parametrize('base_ref', ['stale', 'missing', 'recorded'])
@pytest.mark.parametrize('body,verdict,tails', [
    ('print(json.dumps(r))', True, ['ok', 'ok']),
    ('r["exit_code"]=7;print(json.dumps(r))', False, ['ok']),
    ('subprocess.run(["git","-C",repo,"-c","user.name=test","-c","user.email=t@e","commit","--allow-empty","-m","drift"],stdout=sys.stderr,check=True);print(json.dumps(r))', False, None),
    ('r.update(not_started=True,duration_s=0,tail="");print(json.dumps(r))', True, ['one', 'two']),
    *[(body, False, None) for body in [
        '', '''print('{"x":' * 100000 + "0" + "}" * 100000)''', 'print("{")', 'print(json.dumps(r));print(json.dumps(r))',
        'del r["exit_code"];print(json.dumps(r))', 'del r["worktree"];print(json.dumps(r))',
        'r["duration_s"]=float("nan");print(json.dumps(r))',
        'r["exit_code"]=True;print(json.dumps(r))',
        'r["network"]=1;print(json.dumps(r))', 'r["network"]=False;print(json.dumps(r))',
        'print(json.dumps(r));sys.exit(1)', 'r["not_started"]=True;print(json.dumps(r))',
        *[f'r["{field}"]="wrong";print(json.dumps(r))' for field in ('head', 'base', 'command', 'env_sha256', 'network', 'worktree')],
    ]],
])
def test_runner(git_repo, tmp_path, bwrap_log, body, verdict, tails, base_ref):  # noqa: F811
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
    body = (f'repo=r["worktree"]\nassert r["base"] == "{base}"\n'
            'assert not Path.cwd().is_relative_to(Path(repo))\n'
            f'assert r.pop("env") == {dict(sandbox.SANDBOX_ENV, UV_CACHE_DIR="/operator/cache")!r}\n'
            'assert r["network"] is True\n'
            'assert r["env_sha256"] == hashlib.sha256(json.dumps('
            f'{dict(sandbox.SANDBOX_ENV, UV_CACHE_DIR="/operator/cache")!r},'
            'sort_keys=True,separators=(",", ":")).encode()).hexdigest()\n' + body)
    path = tmp_path / 'runner.py'
    path.write_text('import hashlib,json,sys,subprocess\nfrom pathlib import Path\nr=json.load(sys.stdin)\nr.update(exit_code=0,duration_s=0.1,not_started=False,tail="ok")\n' + body)
    calls = []
    report = sandbox.run_checks(make_config(checks=('echo one', 'echo two'),
                                env=(("UV_CACHE_DIR", "/operator/cache"),), network=True,
                                check_runner=(sys.executable, str(path))), git_repo, progress=calls.append)
    assert report.ok is verdict
    if 'exit_code"]=7' in body:
        assert report.results[0].exit_code == 7
    if tails is not None:
        assert [r.tail for r in report.results] == tails
    if not verdict:
        assert sum(r.tail == 'ok' for r in report.results) <= 1
    if '* 100000' in body:
        assert 'Stage runner failed' in report.results[0].tail
        assert any(word in report.results[0].tail.lower() for word in ('recursion', 'stack overflow'))
    if 'drift' in body:
        assert report.dirty and 'Worktree changed before stage' in report.results[1].tail
    elif not verdict:
        assert calls == ['echo one']
    _save_report(git_repo, report)
    assert _load_report(git_repo) == report
    assert set(json.loads((git_repo / '.loopzero/checks.json').read_text())) == {'head', 'dirty', 'results'}


@pytest.mark.parametrize('dirty', [True, False])
def test_runner_unavailable(git_repo, monkeypatch, dirty):
    if dirty:
        (git_repo / 'dirty').write_text('x')
    git(git_repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    monkeypatch.setattr(sandbox.shutil, 'which', lambda name: None)
    prefix = (sys.executable, '-c', 'import json,sys;r=json.load(sys.stdin);r.pop("env");r.update(exit_code=0,duration_s=0,not_started=True,tail="");print(json.dumps(r))')
    report = sandbox.run_checks(make_config(check_runner=prefix), git_repo)
    assert not report.ok and report.dirty is dirty
    assert ('bwrap binary missing' in report.results[0].tail) is not dirty


@pytest.mark.parametrize('value', ['[]', '[""]', '[1]', '"runner"', '["./runner"]', '["tools/runner"]', '["python3","tools/run_stage.py"]', '["dispatch","--stage"]', '["dispatch","--opt=/operator/run.py"]'])
def test_runner_config(tmp_path, value):
    path = tmp_path / 'workflow.toml'
    path.write_text('[repo]\nname="o/n"\n[checks]\nrunner=' + value)
    if 'dispatch' in value or 'tools/run_stage.py' in value:
        assert load(path).check_runner == tuple(json.loads(value))
    else:
        with pytest.raises(ConfigError):
            load(path)


@pytest.mark.parametrize('kind', ['executable', 'argument', 'symlink', 'path', 'executable_symlink', 'option', 'option_symlink'])
def test_runner_rejects_task_paths(git_repo, tmp_path, monkeypatch, kind):
    runner = git_repo / 'runner'
    runner.write_text('#!/bin/sh\nexit 0\n')
    runner.chmod(0o755)
    git(git_repo, 'add', 'runner')
    git(git_repo, 'commit', '-m', 'runner')
    git(git_repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    link = tmp_path / 'runner-link'
    link.symlink_to(runner)
    monkeypatch.setenv('PATH', f'{git_repo}:' + os.environ['PATH'])
    prefix = {'executable': (str(runner),), 'argument': (sys.executable, str(runner)),
              'symlink': (sys.executable, str(link)), 'path': ('runner',),
              'executable_symlink': (str(link),), 'option': (sys.executable, f'--script={runner}'),
              'option_symlink': (sys.executable, f'--script={link}')}[kind]
    report = sandbox.run_checks(make_config(check_runner=prefix), git_repo)
    assert not report.ok and 'references task worktree' in report.results[0].tail


@pytest.mark.parametrize('bare', [False, True])
def test_runner_preserves_executable_symlink(git_repo, tmp_path, monkeypatch, bare):
    link = tmp_path / 'runner-python'
    link.symlink_to(sys.executable)
    monkeypatch.setenv('PATH', f'{tmp_path}:' + os.environ['PATH'])
    git(git_repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    body = (f'import json,sys;assert sys.executable == {str(link)!r};'
            'r=json.load(sys.stdin);r.pop("env");'
            'r.update(exit_code=0,duration_s=0,not_started=False,tail="ok");print(json.dumps(r))')
    report = sandbox.run_checks(make_config(check_runner=(link.name if bare else str(link), '-c', body, 'relative/argument')), git_repo)
    assert report.ok, report.results[0].tail
