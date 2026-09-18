from __future__ import annotations

import sys

import pytest

from loopzero import _proc


def test_run_captures_output_exit_code_and_duration(tmp_path):
    done = _proc.run(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        cwd=tmp_path,
        env_allowlist=("PATH",),
        timeout=30,
    )
    assert (done.exit_code, done.stdout, done.stderr) == (3, "out\n", "err\n")
    assert done.duration_s >= 0


def test_run_strips_environment_to_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("LZ_SECRET", "nope")
    monkeypatch.setenv("LZ_KEEP", "yes")
    done = _proc.run(
        [sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.startswith('LZ_')))"],
        cwd=tmp_path,
        env_allowlist=("PATH", "LZ_KEEP", "LZ_ABSENT"),
        extra_env={"LZ_EXTRA": "1"},
        timeout=30,
    )
    assert done.stdout.strip() == "['LZ_EXTRA', 'LZ_KEEP']"


def test_run_uses_cwd(tmp_path):
    argv = [sys.executable, "-c", "import os; print(os.getcwd())"]
    done = _proc.run(argv, cwd=tmp_path, env_allowlist=(), timeout=30)
    assert done.stdout.strip() == str(tmp_path.resolve())


def test_run_never_uses_a_shell(tmp_path):
    marker = tmp_path / "pwned"
    done = _proc.run(["true", ";", "touch", str(marker)], cwd=tmp_path, env_allowlist=("PATH",), timeout=30)
    assert done.exit_code == 0
    assert not marker.exists()


def test_run_raises_tool_missing(tmp_path):
    with pytest.raises(_proc.ToolMissing, match="definitely-not-a-tool"):
        _proc.run(["definitely-not-a-tool"], cwd=tmp_path, env_allowlist=("PATH",), timeout=30)


def test_run_raises_on_timeout(tmp_path):
    with pytest.raises(_proc.ProcTimeout, match="timed out"):
        _proc.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            env_allowlist=("PATH",),
            timeout=0.2,
        )


def test_merge_output_interleaves_streams(tmp_path):
    done = _proc.run(
        [sys.executable, "-u", "-c", "import sys; print('a'); print('b', file=sys.stderr); print('c')"],
        cwd=tmp_path,
        env_allowlist=("PATH",),
        timeout=30,
        merge_output=True,
    )
    assert done.stdout.splitlines() == ["a", "b", "c"]
    assert done.stderr == ""


def test_tail_keeps_last_lines():
    text = "\n".join(str(i) for i in range(100)) + "\n"
    assert _proc.tail(text).splitlines() == [str(i) for i in range(60, 100)]
    assert _proc.tail("", 3) == ""
