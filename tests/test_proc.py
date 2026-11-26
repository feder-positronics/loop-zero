from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from loopzero import _proc


def test_unwind_on_sigterm_when_signal_arrives_during_disposition_restore():
    script = """
import signal
from loopzero import _proc

set_disposition = signal.signal

def deliver_before_restore(signum, disposition):
    if signum == signal.SIGTERM and disposition is signal.SIG_DFL and callable(signal.getsignal(signum)):
        signal.raise_signal(signum)
    return set_disposition(signum, disposition)

signal.signal = deliver_before_restore
with _proc.unwind_on_sigterm():
    pass
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=5)
    assert result.returncode == -signal.SIGTERM, result.stderr.decode()


@pytest.fixture
def run_python(tmp_path):
    def run(script, *, timeout=30, env_allowlist=("PATH",), unbuffered=False, **kwargs):
        python = [sys.executable, "-u"] if unbuffered else [sys.executable]
        return _proc.run([*python, "-c", script], cwd=tmp_path, env_allowlist=env_allowlist,
                         timeout=timeout, **kwargs)

    return run


def test_run_captures_output_exit_code_and_duration(run_python):
    done = run_python("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
    assert (done.exit_code, done.stdout, done.stderr) == (3, "out\n", "err\n")
    assert done.duration_s >= 0


def test_run_strips_environment_to_allowlist(run_python, monkeypatch):
    monkeypatch.delenv("LZ_ABSENT", raising=False)
    monkeypatch.setenv("LZ_SECRET", "nope")
    monkeypatch.setenv("LZ_KEEP", "yes")
    done = run_python("import os; print(sorted(k for k in os.environ if k.startswith('LZ_')))",
                      env_allowlist=("PATH", "LZ_KEEP", "LZ_ABSENT"), extra_env={"LZ_EXTRA": "1"})
    assert done.stdout.strip() == "['LZ_EXTRA', 'LZ_KEEP']"


def test_run_uses_cwd(tmp_path, run_python):
    done = run_python("import os; print(os.getcwd())", env_allowlist=())
    assert done.stdout.strip() == str(tmp_path.resolve())


def test_run_never_uses_a_shell(tmp_path):
    marker = tmp_path / "pwned"
    done = _proc.run(["true", ";", "touch", str(marker)], cwd=tmp_path, env_allowlist=("PATH",), timeout=30)
    assert done.exit_code == 0
    assert not marker.exists()


def test_run_raises_tool_missing(tmp_path):
    with pytest.raises(_proc.ToolMissing, match="definitely-not-a-tool"):
        _proc.run(["definitely-not-a-tool"], cwd=tmp_path, env_allowlist=("PATH",), timeout=30)


def test_run_raises_on_timeout(run_python):
    with pytest.raises(_proc.ProcTimeout, match="timed out"):
        run_python("import time; time.sleep(5)", timeout=0.2)


def test_run_reaps_child_before_propagating_keyboard_interrupt(run_python, monkeypatch):
    children = []

    class InterruptedPopen(subprocess.Popen):
        def _communicate(self, *args, **kwargs):
            children.append(self)
            raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", InterruptedPopen)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_python("import time; time.sleep(30)", timeout=5)
        assert children[0].returncode is not None
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="checks Linux process state")
def test_timeout_stops_descendant_and_preserves_output(tmp_path, monkeypatch):
    wait = "import signal; signal.pause()"
    child = f"import os; print(os.getpid(), flush=True); {wait}"
    parent = (
        "import subprocess, sys; print('parent stderr', file=sys.stderr, flush=True); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); {wait}"
    )

    class ReadyPopen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            # Wait for the flushed PID without consuming the captured output.
            assert select.select([self.stdout], [], [], 5)[0], "descendant did not publish its PID"
            return super().communicate(input, timeout=timeout)

    with subprocess.Popen([sys.executable, "-c", wait]) as unrelated:
        monkeypatch.setattr(subprocess, "Popen", ReadyPopen)
        try:
            with pytest.raises(_proc.ProcTimeout) as raised:
                _proc.run(
                    [sys.executable, "-c", parent], cwd=tmp_path, env_allowlist=(), timeout=0.75,
                )
            pid = int(raised.value.stdout)
            stat = Path(f"/proc/{pid}/stat")
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        state = stat.read_text().rsplit(")", 1)[1].split()[0]
                    except (FileNotFoundError, ProcessLookupError):
                        state = "gone"
                    if state in {"gone", "Z", "X"}:
                        break
                    assert time.monotonic() < deadline, f"descendant did not exit: {state}"
                    time.sleep(0.01)
                assert raised.value.stderr == "parent stderr\n"
                assert unrelated.poll() is None
            finally:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        finally:
            unrelated.kill()


def test_merge_output_interleaves_streams(run_python):
    done = run_python("import sys; print('a'); print('b', file=sys.stderr); print('c')",
                      unbuffered=True, merge_output=True)
    assert done.stdout.splitlines() == ["a", "b", "c"]
    assert done.stderr == ""


def test_tail_keeps_last_lines():
    text = "\n".join(str(i) for i in range(100)) + "\n"
    assert _proc.tail(text).splitlines() == [str(i) for i in range(60, 100)]
    assert _proc.tail("", 3) == ""


def test_explicit_utf8_input_under_ascii_parent_locale(tmp_path):
    source = str(Path(_proc.__file__).resolve().parents[1])
    script = r"""
import locale, sys
from loopzero import _proc
assert locale.getencoding() in ('ANSI_X3.4-1968', 'US-ASCII')
argv = [sys.executable, '-c', 'import sys; print(sys.stdin.buffer.read().hex())']
options = dict(cwd='.', env_allowlist=(), timeout=30)
assert _proc.run(argv, input='ascii', **options).stdout.strip() == '6173636969'
assert _proc.run(argv, input='\u03a9', **options).stdout.strip() == '3f'
print(_proc.run(argv, input='\u03a9\r\n\u2028\r', encoding='utf-8', **options).stdout.strip())
"""
    done = _proc.run(
        [sys.executable, "-c", script], cwd=tmp_path, env_allowlist=(), timeout=30,
        extra_env={"PYTHONPATH": source, "LC_ALL": "C", "PYTHONUTF8": "0",
                   "PYTHONCOERCECLOCALE": "0"},
    )
    assert done.exit_code == 0, done.stderr
    assert done.stdout.strip() == "Ω\r\n\u2028\r".encode("utf-8").hex()
