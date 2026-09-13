"""Issue 4302 supervision failure, frozen binding, and archive contracts."""

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from .test_job_runner import (
    _instrumented_job_script,
    _isolate_job_runner_from_parent_worktree_lease as _isolate_job_runner_from_parent_worktree_lease,
    _job,
    _lease_path,
    _write_orphaned_v2_binding,
)


@pytest.mark.parametrize("case", ["malformed", "mismatch", "match"])
def test_native_digest_option_preserves_frozen_binding(
    tmp_path: Path, case: str
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")
    # Preserve formatting too: the contract binds original bytes, not parsed JSON.
    binding_path = job_dir / "binding.json"
    binding_path.write_bytes(binding_path.read_bytes() + b"\n ")
    before = {p.name: p.read_bytes() for p in job_dir.iterdir() if p.is_file()}
    expected = hashlib.sha256(before["binding.json"]).hexdigest()
    supplied = {"malformed": "A" * 64, "mismatch": "0" * 64, "match": expected}[case]

    result = _job(
        tmp_path, "reconcile", binding["name"], "--expected-binding-sha256", supplied
    )

    for name, payload in before.items():
        assert (job_dir / name).read_bytes() == payload
    if case == "match":
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["schema_version"] == "job-supervisor-loss-v1"
        assert receipt["binding_sha256"] == expected
        replay = _job(tmp_path, "reconcile", binding["name"])
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout) == receipt
    else:
        assert result.returncode != 0
        assert (
            "--expected-binding-sha256 requires 64 lowercase hexadecimal characters"
            if case == "malformed"
            else "job binding does not match expected SHA-256"
        ) in result.stderr
        assert {
            p.name: p.read_bytes() for p in job_dir.iterdir() if p.is_file()
        } == before


@pytest.mark.parametrize(
    "case",
    [
        "eligible",
        "other-exit",
        "malformed-exit",
        "v1",
        "partial-envelope",
        "partial-digest",
        "dangling-envelope",
        "dangling-digest",
        "foreign-path",
        "stale-binding",
        "foreign-name",
        "malformed-binding",
        "live-lease",
        "no-digest",
    ],
)
def test_native_exit125_state_preserves_originals(tmp_path: Path, case: str) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")
    (job_dir / "exit_code").write_bytes(b"125\n")
    (job_dir / "terminal-envelope.json.tmp").write_bytes(b"")
    (job_dir / "terminal-envelope.sha256.tmp").write_bytes(b"")
    expected = hashlib.sha256((job_dir / "binding.json").read_bytes()).hexdigest()
    if case == "other-exit":
        (job_dir / "exit_code").write_bytes(b"1\n")
    elif case == "malformed-exit":
        (job_dir / "exit_code").write_bytes(b"not-an-exit\n")
    elif case in {"v1", "foreign-path", "foreign-name", "malformed-binding"}:
        if case == "v1":
            binding["schema_version"] = "job-binding-v1"
            binding["terminal_artifact"] = binding.pop("terminal_envelope")
        elif case == "foreign-path":
            binding["terminal_envelope"] = str(tmp_path / "foreign.json")
        elif case == "foreign-name":
            binding["name"] = "foreign"
        else:
            binding["run_id"] = "invalid"
        (job_dir / "binding.json").write_text(json.dumps(binding))
        expected = hashlib.sha256((job_dir / "binding.json").read_bytes()).hexdigest()
    elif case == "stale-binding":
        (job_dir / "binding.json").write_bytes(
            (job_dir / "binding.json").read_bytes() + b" "
        )
    elif case.startswith("partial-") or case.startswith("dangling-"):
        name = (
            "terminal-envelope.json"
            if case.endswith("envelope")
            else "terminal-envelope.sha256"
        )
        if case.startswith("dangling-"):
            (job_dir / name).symlink_to(tmp_path / "absent")
        else:
            (job_dir / name).write_bytes(b"{}")

    def snapshot() -> dict[str, bytes | str]:
        return {
            p.name: os.readlink(p) if p.is_symlink() else p.read_bytes()
            for p in job_dir.iterdir()
        }

    before = snapshot()
    options = [] if case == "no-digest" else ["--expected-binding-sha256", expected]
    if case == "v1":
        options += ["--terminal-artifact", binding["terminal_artifact"]]
    lease_fd = None
    try:
        if case == "live-lease":
            lease_fd = os.open(_lease_path(tmp_path, binding["name"]), os.O_RDWR)
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _job(tmp_path, "reconcile", job_dir.name, *options)
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
    if case == "eligible":
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["failure_class"] == "terminal-supervision-failure"
        assert receipt["observed_exit_code"] == 125
        after = snapshot()
        assert after.pop("reconciliation.json")
        assert after == before
        return
    assert result.returncode != 0
    if case.startswith("dangling-"):
        assert "durable terminal files cannot be symlinks" in result.stderr
    elif case.startswith("partial-"):
        assert "conflicting partial durable terminal files" in result.stderr
    elif case == "stale-binding":
        assert "job binding does not match expected SHA-256" in result.stderr
    elif case == "live-lease":
        assert "active launcher or supervisor" in result.stderr
    elif case == "no-digest":
        assert "requires --expected-binding-sha256" in result.stderr
    assert snapshot() == before
    assert not (job_dir.parent / ".failure-archives").exists()


@pytest.mark.parametrize(
    "case",
    [
        "clean",
        "replay",
        "tamper",
        "receipt-conflict",
        "receipt-reservation",
        "archive-link",
        "original-link",
    ],
)
def test_native_failure_archive_receipt(tmp_path: Path, case: str) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")
    (job_dir / "exit_code").write_bytes(b"125\n")
    (job_dir / "log").write_bytes(b"original\x00failure\n")
    (job_dir / "terminal-envelope.json.tmp").write_bytes(b"")
    expected = hashlib.sha256((job_dir / "binding.json").read_bytes()).hexdigest()
    original = {p.name: p.read_bytes() for p in job_dir.iterdir()}
    archive_root = job_dir.parent / ".failure-archives"
    if case == "receipt-conflict":
        (job_dir / "reconciliation.json").write_bytes(b"{}\n")
    elif case == "receipt-reservation":
        (job_dir / "reconciliation.json.tmp").write_bytes(b"reserved")
    elif case == "archive-link":
        archive_root.symlink_to(tmp_path / "foreign", target_is_directory=True)
    elif case == "original-link":
        (job_dir / "extra").symlink_to(tmp_path / "absent")
    result = _job(
        tmp_path, "reconcile", binding["name"], "--expected-binding-sha256", expected
    )
    for name, payload in original.items():
        assert (job_dir / name).read_bytes() == payload
    if case in {
        "receipt-conflict",
        "receipt-reservation",
        "archive-link",
        "original-link",
    }:
        assert result.returncode != 0
        assert not archive_root.exists()
        return
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert set(receipt) == {
        "schema_version",
        "name",
        "run_id",
        "task_id",
        "failure_class",
        "observed_exit_code",
        "binding_sha256",
        "archive_sha256",
    }
    assert receipt["schema_version"] == "job-supervision-failure-v1"
    assert receipt["observed_exit_code"] == 125
    assert receipt["failure_class"] == "terminal-supervision-failure"
    archive = archive_root / receipt["archive_sha256"]
    manifest_bytes = (archive / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == receipt["archive_sha256"]
    assert {
        p.name: p.read_bytes() for p in (archive / "original").iterdir()
    } == original
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    if case == "tamper":
        (archive / "original/log").write_bytes(b"tampered")
        replay = _job(
            tmp_path,
            "reconcile",
            binding["name"],
            "--expected-binding-sha256",
            expected,
        )
        assert replay.returncode != 0
        assert "archive" in replay.stderr
        assert _job(tmp_path, "clean").returncode == 0
        assert job_dir.exists()
    elif case == "replay":
        replay = _job(
            tmp_path,
            "reconcile",
            binding["name"],
            "--expected-binding-sha256",
            expected,
        )
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout) == receipt
        assert list(archive_root.iterdir()) == [archive]
    else:
        assert _job(tmp_path, "clean").returncode == 0
        assert not job_dir.exists()
        assert (archive / "original/exit_code").read_bytes() == b"125\n"


@pytest.mark.parametrize("cut", ["partial-archive", "durable-archive"])
def test_native_failure_archive_crash_cut(tmp_path: Path, cut: str) -> None:
    marker = (
        '            write_at(archive_fd, "manifest.json", manifest_bytes)'
        if cut == "partial-archive"
        else "            os.fsync(archives_fd)"
    )
    script = _instrumented_job_script(
        tmp_path,
        marker=marker,
        replacement=(
            "            os._exit(97)\n" + marker
            if cut == "partial-archive"
            else marker + "\n            os._exit(97)"
        ),
    )
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")
    (job_dir / "exit_code").write_bytes(b"125\n")
    originals = {p.name: p.read_bytes() for p in job_dir.iterdir()}
    expected = hashlib.sha256(originals["binding.json"]).hexdigest()
    first = _job(
        tmp_path,
        "reconcile",
        binding["name"],
        "--expected-binding-sha256",
        expected,
        script=script,
    )
    assert first.returncode == 97
    assert not (job_dir / "reconciliation.json").exists()
    archive_root = job_dir.parent / ".failure-archives"
    (archive,) = archive_root.iterdir()
    replay = _job(
        tmp_path, "reconcile", binding["name"], "--expected-binding-sha256", expected
    )
    if cut == "partial-archive":
        assert replay.returncode != 0
        assert "archive" in replay.stderr
        assert not (job_dir / "reconciliation.json").exists()
    else:
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout)["archive_sha256"] == archive.name
    assert list(archive_root.iterdir()) == [archive]
    for name, payload in originals.items():
        assert (job_dir / name).read_bytes() == payload


@pytest.mark.parametrize("prior_waited", [False, True])
def test_completed_failure_wait_preserves_archive(
    tmp_path: Path, prior_waited: bool
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")
    (job_dir / "exit_code").write_bytes(b"125\n")
    (job_dir / "log").write_bytes(b"original\x00failure\n")
    if prior_waited:
        (job_dir / "waited").write_bytes(b"original marker\n")
    expected = hashlib.sha256((job_dir / "binding.json").read_bytes()).hexdigest()
    args = ("reconcile", binding["name"], "--expected-binding-sha256", expected)
    first = _job(tmp_path, *args)
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    archive = job_dir.parent / ".failure-archives" / receipt["archive_sha256"]
    original = {p.name: p.read_bytes() for p in job_dir.iterdir()}
    archived = {
        str(p.relative_to(archive)): p.read_bytes()
        for p in archive.rglob("*")
        if p.is_file()
    }
    assert _job(tmp_path, "check").returncode == 0
    for _ in range(2):
        waited = _job(tmp_path, "wait", binding["name"], "--timeout", "300")
        assert waited.returncode == 125, waited.stderr
        assert waited.stderr == ""
        assert {p.name: p.read_bytes() for p in job_dir.iterdir()} == original
        assert {
            str(p.relative_to(archive)): p.read_bytes()
            for p in archive.rglob("*")
            if p.is_file()
        } == archived
        assert _job(tmp_path, "check").returncode == 0
        replay = _job(tmp_path, *args)
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout) == receipt


@pytest.mark.parametrize("damage", ["missing", "forged", "archive", "original"])
def test_failure_wait_refuses_invalid_receipt_without_mutation(
    tmp_path: Path, damage: str
) -> None:
    job_dir, binding = _write_orphaned_v2_binding(tmp_path, pid="999998")
    (job_dir / "exit_code").write_bytes(b"125\n")
    expected = hashlib.sha256((job_dir / "binding.json").read_bytes()).hexdigest()
    result = _job(
        tmp_path, "reconcile", binding["name"], "--expected-binding-sha256", expected
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    receipt_path = job_dir / "reconciliation.json"
    if damage == "missing":
        receipt_path.unlink()
    elif damage == "forged":
        receipt["schema_version"] = "job-reconciliation-v2"
        receipt_path.write_text(json.dumps(receipt))
    elif damage == "archive":
        archive = job_dir.parent / ".failure-archives" / receipt["archive_sha256"]
        (archive / "original" / "exit_code").write_bytes(b"0\n")
    else:
        (job_dir / "extra").write_bytes(b"unexpected original")
    before = {p.name: p.read_bytes() for p in job_dir.iterdir()}
    waited = _job(tmp_path, "wait", binding["name"], "--timeout", "300")
    assert waited.returncode == 125
    assert "supervision failure receipt is invalid" in waited.stderr
    assert {p.name: p.read_bytes() for p in job_dir.iterdir()} == before
