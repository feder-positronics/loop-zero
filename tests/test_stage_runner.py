import json
import sys

import pytest

from loopzero import sandbox

from .test_sandbox import bwrap_log, make_config  # noqa: F401


def runner(tmp_path, body):
    path = tmp_path / 'runner.py'
    path.write_text('import json,sys,subprocess\nr=json.load(sys.stdin)\nr.update(exit_code=0,duration_s=0.1,not_started=False,tail="ok")\n' + body)
    return (sys.executable, str(path))


def test_pass(git_repo, tmp_path):
    config = make_config(checks=('echo one', 'echo two'),
                         check_runner=runner(tmp_path, 'print(json.dumps(r))'))
    report = sandbox.run_checks(config, git_repo)
    assert report.ok and not report.dirty
    assert [(r.command, r.tail) for r in report.results] == [('echo one', 'ok'), ('echo two', 'ok')]


@pytest.mark.parametrize('body', [
    '', 'print("{")', 'r["head"]="wrong";print(json.dumps(r))',
    'r["base"]="wrong";print(json.dumps(r))',
    'r["command"]="wrong";print(json.dumps(r))',
    'print(json.dumps(r));print(json.dumps(r))',
    'del r["exit_code"];print(json.dumps(r))',
    'r["duration_s"]=float("nan");print(json.dumps(r))',
    'r["exit_code"]=True;print(json.dumps(r))',
    'print(json.dumps(r));sys.exit(1)',
    'r["not_started"]=True;print(json.dumps(r))',
])
def test_invalid(git_repo, tmp_path, body):
    report = sandbox.run_checks(make_config(check_runner=runner(tmp_path, body)), git_repo)
    assert not report.ok and len(report.results) == 1


def test_failure_stops(git_repo, tmp_path):
    body = 'assert r["command"] == "first"\nr["exit_code"]=7\nprint(json.dumps(r))'
    report = sandbox.run_checks(make_config(checks=('first', 'later'),
                                           check_runner=runner(tmp_path, body)), git_repo)
    assert [(r.command, r.exit_code) for r in report.results] == [('first', 7)]


def test_drift_stops(git_repo, tmp_path):
    body = 'subprocess.run(["git","-c","user.name=test","-c","user.email=test@example.com","commit","--allow-empty","-m","drift"],stdout=sys.stderr,check=True)\nprint(json.dumps(r))'
    report = sandbox.run_checks(make_config(checks=('first', 'later'),
                                           check_runner=runner(tmp_path, body)), git_repo)
    assert not report.ok and report.dirty
    assert all(r.command != 'later' for r in report.results)


def test_dirty_never_starts(git_repo, tmp_path):
    (git_repo / 'dirty').write_text('x')
    report = sandbox.run_checks(make_config(check_runner=runner(tmp_path, 'raise AssertionError()')), git_repo)
    assert not report.ok and report.dirty


def test_fallback(git_repo, tmp_path, bwrap_log):  # noqa: F811
    body = 'r.update(not_started=True,duration_s=0,tail="")\nprint(json.dumps(r))'
    report = sandbox.run_checks(make_config(check_runner=runner(tmp_path, body)), git_repo)
    assert report.ok and report.results[0].tail == 'hello'


def test_receipt_roundtrip(git_repo, tmp_path):
    from loopzero.cli import _load_report, _save_report
    report = sandbox.run_checks(make_config(check_runner=runner(tmp_path, 'print(json.dumps(r))')), git_repo)
    _save_report(git_repo, report)
    assert _load_report(git_repo) == report
    data = json.loads((git_repo / '.loopzero/checks.json').read_text())
    assert set(data) == {'head', 'dirty', 'results'}


@pytest.mark.parametrize('value', ['[]', '[""]', '[1]', '"runner"'])
def test_invalid_runner_config(tmp_path, value):
    from loopzero.config import ConfigError, load
    path = tmp_path / 'workflow.toml'
    path.write_text('[repo]\nname="o/n"\n[checks]\nrunner=' + value)
    with pytest.raises(ConfigError):
        load(path)


def test_runner_config(tmp_path):
    from loopzero.config import load
    path = tmp_path / 'workflow.toml'
    path.write_text('[repo]\nname="o/n"\n[checks]\nrunner=["dispatch", "--stage"]')
    assert load(path).check_runner == ('dispatch', '--stage')


def test_fallback_unavailable_is_red(git_repo, tmp_path, monkeypatch):
    body = 'r.update(not_started=True,duration_s=0,tail="")\nprint(json.dumps(r))'
    monkeypatch.setattr(sandbox.shutil, 'which', lambda name: None)
    report = sandbox.run_checks(make_config(check_runner=runner(tmp_path, body)), git_repo)
    assert not report.ok
    assert 'Local fallback failed' in report.results[0].tail
