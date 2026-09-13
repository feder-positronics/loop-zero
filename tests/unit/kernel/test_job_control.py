"""Stop requests authenticate durable ownership before sending any signal."""

import json
import os
from pathlib import Path

import pytest

from loopzero.kernel import job_control


@pytest.mark.parametrize("damage", ["boot", "start", "lease", "name", "pid", "legacy", "pid-link", "identity-link"])
def test_stop_refuses_stale_or_substituted_identity(tmp_path: Path, monkeypatch, damage: str) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    lease = tmp_path / "owned.lock"
    lease.touch()
    metadata = lease.stat()
    pid = os.getpid()
    identity = {
        "schema_version": "job-pid-identity-v2", "name": directory.name, "pid": pid,
        "boot_id": job_control.BOOT_ID.read_text().strip(),
        "starttime": Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[19],
        "lease_dev": metadata.st_dev, "lease_ino": metadata.st_ino,
    }
    if damage == "boot":
        identity["boot_id"] = "another-boot"
    elif damage == "start":
        identity["starttime"] = "0"
    elif damage == "lease":
        identity["lease_ino"] += 1
    elif damage == "name":
        identity["name"] = "foreign"
    elif damage == "pid":
        identity["pid"] += 1
    elif damage == "legacy":
        identity["schema_version"] = "job-pid-identity-v1"
        del identity["boot_id"]
    (directory / "pid").write_text(str(pid))
    (directory / "pid-identity.json").write_text(json.dumps(identity))
    if damage.endswith("-link"):
        filename = "pid" if damage == "pid-link" else "pid-identity.json"
        (directory / filename).rename(tmp_path / "foreign")
        (directory / filename).symlink_to(tmp_path / "foreign")
    monkeypatch.setattr(job_control, "pidfd_send_signal", lambda *_: pytest.fail("must not signal stale identity"))
    with pytest.raises((ValueError, OSError)):
        job_control.request_stop(directory, lease, directory.name)


@pytest.mark.parametrize("damage", [None, "binding", "exit", "envelope", "receipt-link"])
def test_timeout_receipt_binds_original_job_and_refuses_conflicting_evidence(tmp_path: Path, damage: str | None) -> None:
    import hashlib
    binding = {
        "schema_version": "job-binding-v2", "name": "timeout",
        "run_id": "sr_" + "a" * 32, "task_id": "final-ci-repro",
        "terminal_envelope": str(tmp_path / "terminal-envelope.json"),
    }
    binding_bytes = json.dumps(binding).encode()
    (tmp_path / "binding.json").write_bytes(binding_bytes)
    (tmp_path / "exit_code").write_bytes(b"124\n")
    receipt = {
        "schema_version": "job-supervision-timeout-v1", "name": binding["name"],
        "run_id": binding["run_id"], "task_id": binding["task_id"],
        "failure_class": "supervision-timeout", "exit_code": 124,
        "binding_sha256": hashlib.sha256(binding_bytes).hexdigest(),
    }
    (tmp_path / "reconciliation.json").write_text(json.dumps(receipt))
    if damage == "binding":
        (tmp_path / "binding.json").write_bytes(binding_bytes + b" ")
    elif damage == "exit":
        (tmp_path / "exit_code").write_bytes(b"0\n")
    elif damage == "envelope":
        (tmp_path / "terminal-envelope.json").symlink_to(tmp_path / "absent")
    elif damage == "receipt-link":
        (tmp_path / "reconciliation.json").rename(tmp_path / "foreign")
        (tmp_path / "reconciliation.json").symlink_to(tmp_path / "foreign")
    if damage:
        with pytest.raises((ValueError, OSError)):
            job_control.timeout_receipt(tmp_path)
    else:
        assert job_control.timeout_receipt(tmp_path) == receipt
        with pytest.raises(ValueError, match="binding mismatch"):
            job_control.timeout_receipt(tmp_path, expected_binding_sha256="0" * 64)
