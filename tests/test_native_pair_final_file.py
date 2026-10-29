"""Actual Codex adapter with fake process/sandbox; no native or network calls."""

import base64
import hashlib
import json
import os
import socket
import subprocess
import tempfile

import pytest

from loopzero import _proc, cli, native_pair, runners
from tests.test_native_pair import BASE, HEAD, configuration, eligible, published
from tests.test_native_pair_repair import DIFF, invoke_cli, review_inputs
from tests.test_runners import _native_codex_result


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    calls, homes, expected = [], [], {}
    binary = tmp_path / "fake-binary"
    binary.write_text("FAKE, NEVER EXECUTED")
    real_read = os.read
    real_temporary = tempfile.TemporaryDirectory

    def denied(*args, **kwargs):
        raise AssertionError("No real subprocess/network/native execution")

    def temporary(*args, **kwargs):
        return real_temporary(*args, **kwargs, dir=tmp_path)

    def sandbox_prefix(family, cwd, home, paths, binds):
        homes.append(home)
        return [], configuration(), binary

    def install(mode):
        reads = []

        def file_read(fd, count):
            if os.readlink(f"/proc/self/fd/{fd}").endswith("/last.json"):
                reads.append(count)
                if mode == "read_failure":
                    if len(reads) > 1:
                        raise OSError("synthetic partial read")
                    return real_read(fd, min(8192, count))
                if mode == "growing":
                    with (homes[-1] / "last.json").open("ab") as changed:
                        changed.write(b"EXTRA")
            return real_read(fd, count)

        monkeypatch.setattr(os, "read", file_read)

        def process(argv, **kwargs):
            model = argv[argv.index("-m") + 1]
            calls.append(model)
            native = _native_codex_result(
                HEAD, base=BASE, patch=DIFF, cwd=str(tmp_path), session="fake-" + model
            )
            raw = json.loads(native.raw)
            message = raw["final_message"].encode()
            if mode == "conflict":
                message = json.dumps({"verdict": "request_changes", "findings": [{
                    "severity": "important", "path": None, "line": None,
                    "title": "conflict", "body": "PRIVATE" + "q" * 45000,
                }]}).encode()
            elif mode in {"malformed", "timeout", "nonzero", "read_failure", "growing", "oversize"}:
                message = b"PRIVATE-BEGIN" + b"q" * 45000 + b"PRIVATE-END"
            elif mode == "invalid_utf8":
                message = b"PRIVATE-BEGIN" + b"q" * 45000 + b"\xff"
            if mode == "oversize":
                message = b"PRIVATE" + b"q" * 1_100_000
            final = homes[-1] / "last.json"
            if mode == "unreadable":
                final.symlink_to(binary)
            elif mode != "absent":
                final.write_bytes(message)
            stdout = "\n".join(json.dumps(e) for e in raw["events"]) + "\n"
            stderr = f"model: {model}\nreasoning effort: medium\n"
            expected[model] = (message, stdout, stderr)
            if mode == "timeout":
                raise _proc.ProcTimeout("fake timeout", stdout, stderr=stderr, duration_s=2.5)
            return _proc.Completed(71 if mode == "nonzero" else 0, stdout, stderr, .1)

        monkeypatch.setattr(_proc, "run", process)

    review_inputs(monkeypatch)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(runners, "_auth_binds", lambda *a: ())
    monkeypatch.setattr(runners, "_sandbox_prefix", sandbox_prefix)
    monkeypatch.setattr(runners, "_verify_codex_instruction_channel", lambda *a: None)
    monkeypatch.setattr(runners, "_codex_supports_ignore_user_config", lambda *a: False)
    return install, calls, homes, expected


@pytest.mark.parametrize("mode", [
    "malformed", "conflict", "invalid_utf8", "timeout", "nonzero", "absent", "unreadable",
    "read_failure", "growing", "oversize",
])
def test_actual_adapter_failed_final_file_retained_before_cleanup(adapter, tmp_path, mode):
    install, calls, homes, expected = adapter
    install(mode)
    with pytest.raises(runners.RunnerBadOutput):
        invoke_cli(tmp_path)
    assert calls == [native_pair.MODELS[0]]
    assert not homes[0].exists()
    files = list((tmp_path / ".loopzero").glob("*-native.json"))
    assert len(files) == 1
    stream = json.loads(files[0].read_text())
    message, stdout, stderr = expected[calls[0]]
    assert (stream["stdout"], stream["stderr"]) == (stdout, stderr)
    assert stream["exit_code"] == (None if mode == "timeout" else 71 if mode == "nonzero" else 0)
    assert stream["duration_s"] == (2.5 if mode == "timeout" else .1)
    assert stream.get("timeout", False) == (mode == "timeout")
    final_path = files[0].with_name(files[0].name.replace("-native.json", "-last.json"))
    final = json.loads(final_path.read_text())
    assert final["attempt"] == stream["attempt"]
    assert final["model"] == stream["model"]
    snapshot = final["output_last_message"]
    if mode in {"absent", "unreadable"}:
        assert snapshot["status"] == mode
        assert "bytes_base64" not in snapshot
    elif mode in {"read_failure", "growing"}:
        assert snapshot["status"] == "partial"
        retained = base64.b64decode(snapshot["bytes_base64"], validate=True)
        assert retained == (message[:8192] if mode == "read_failure" else message)
        assert snapshot["sha256"] == hashlib.sha256(retained).hexdigest()
        assert snapshot["byte_count"] == len(retained)
    else:
        assert snapshot["status"] == "complete"
        assert base64.b64decode(snapshot["bytes_base64"], validate=True) == message
        assert snapshot["sha256"] == hashlib.sha256(message).hexdigest()
        assert snapshot["byte_count"] == len(message) > 44000
    assert final_path.stat().st_mode & 0o777 == 0o600
    assert final_path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads((final_path.parent / stream["attempt"]).read_text())["state"] == "failed"
    with pytest.raises(cli.CliError, match="already"):
        invoke_cli(tmp_path)
    assert calls == [native_pair.MODELS[0]]


def test_actual_adapter_clean_pair_retains_both_files_and_public_privacy(adapter, tmp_path):
    install, calls, homes, expected = adapter
    install("clean")
    result = invoke_cli(tmp_path)
    assert calls == list(native_pair.MODELS)
    assert all(not home.exists() for home in homes)
    assert runners.validate_runner_result(result)
    public = published(result)
    assert eligible([public]) == public
    assert "bytes_base64" not in public["body"]
    assert str(tmp_path) not in public["body"]
    for path in (tmp_path / ".loopzero").glob("*-last.json"):
        record = json.loads(path.read_text())
        assert base64.b64decode(record["output_last_message"]["bytes_base64"]) == expected[record["model"]][0]
    assert len(list((tmp_path / ".loopzero").glob("*-last.json"))) == 2
