"""Authenticated supervisor requests and cancellation of its owned children."""

import ctypes
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import time
from pathlib import Path

from .linux import pidfd_open, pidfd_send_signal

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
STOP_GRACE_S = 2.0
SUPERVISOR_GRACE_S = 10.0


class SupervisionTimeout(Exception):
    """The supervisor has terminated and reaped its command."""


def request_stop(directory: Path | str, lease_path: Path, name: str) -> None:
    """Signal a pinned, current-boot lease owner and await its exit via pidfd.

    No numeric PID is ever a signal target. Keep the directory pinned across
    startup publication and reject links, foreign files and stale identities.
    """
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    pidfd = None
    try:
        def read_file(filename: str) -> bytes:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise ValueError("unsafe supervisor identity file")
                return stream.read(8192)

        deadline = time.monotonic() + 2.0
        while True:
            try:
                identity = json.loads(read_file("pid-identity.json"))
                break
            except (FileNotFoundError, json.JSONDecodeError):
                if time.monotonic() >= deadline:
                    raise ValueError("supervisor identity was not published") from None
                time.sleep(0.01)
        pid = int(read_file("pid"))
        lease = lease_path.lstat()
        if (
            not stat.S_ISREG(lease.st_mode) or lease.st_uid != os.getuid()
            or not isinstance(identity, dict)
            or set(identity) != {"schema_version", "name", "pid", "starttime",
                                 "boot_id", "lease_dev", "lease_ino"}
            or identity["schema_version"] != "job-pid-identity-v2"
            or identity["name"] != name or identity["pid"] != pid or pid <= 1
            or identity["boot_id"] != BOOT_ID.read_text().strip()
            or (identity["lease_dev"], identity["lease_ino"]) != (lease.st_dev, lease.st_ino)
        ):
            raise ValueError("supervisor identity does not match job authority")
        pidfd = pidfd_open(pid)
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
        if fields[19] != identity["starttime"]:
            raise ValueError("supervisor identity is stale")
        pidfd_send_signal(pidfd, signal.SIGTERM)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(int(SUPERVISOR_GRACE_S * 1000)):
            # Killing the sealing authority here would abandon its children.
            raise TimeoutError("supervisor did not finish bounded cancellation")
    finally:
        if pidfd is not None:
            os.close(pidfd)
        os.close(directory_fd)


def run_command(command, arguments, stop_requested) -> int:
    """Own a session until its last cancellation signal, then reap children."""
    from loopzero.runners.process import (
        ProcessHandle, _process_exited_without_reaping, cancel_cli,
    )

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if stop_requested():
        raise SupervisionTimeout()
    child = subprocess.Popen(command, start_new_session=True, **arguments)
    handle = ProcessHandle(process=child, pid=child.pid, pgid=child.pid)
    try:
        while not stop_requested() and not _process_exited_without_reaping(child):
            time.sleep(0.01)
        if not stop_requested():
            return child.wait()
    except BaseException:
        cancel_cli(handle, grace_s=STOP_GRACE_S)
        raise
    # cancel_cli retains the unreaped leader's PID/PGID reservation through
    # TERM and KILL. It never signals a group after consuming that reservation.
    if not cancel_cli(handle, grace_s=STOP_GRACE_S):
        raise RuntimeError("owned command could not be reaped")
    deadline = time.monotonic() + STOP_GRACE_S
    while True:
        try:
            # A PID-namespace boundary can adopt descendants in another
            # session. Reap those too before releasing supervision authority.
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            if time.monotonic() >= deadline:
                raise RuntimeError("owned descendants could not be reaped")
            time.sleep(0.01)
    raise SupervisionTimeout()


def timeout_receipt(directory: Path, *, expected_binding_sha256: str = "") -> dict:
    """Validate a sealed timeout without manufacturing terminal evidence."""
    def read(name):
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ValueError("unsafe timeout evidence")
            return stream.read()

    receipt = json.loads(read("reconciliation.json"))
    if not isinstance(receipt, dict):
        raise ValueError("invalid supervision timeout receipt")
    required = {"schema_version", "name", "failure_class", "exit_code"}
    try:
        binding_bytes = read("binding.json")
    except FileNotFoundError:
        binding_bytes = None
    if binding_bytes is not None:
        binding = json.loads(binding_bytes)
        required |= {"run_id", "task_id", "binding_sha256"}
        digest = hashlib.sha256(binding_bytes).hexdigest()
        if (
            not isinstance(binding, dict)
            or binding.get("schema_version") != "job-binding-v2"
            or any(receipt.get(key) != binding.get(key) for key in ("name", "run_id", "task_id"))
            or receipt.get("binding_sha256") != digest
            or (expected_binding_sha256 and expected_binding_sha256 != digest)
        ):
            raise ValueError("timeout binding mismatch")
    elif expected_binding_sha256:
        raise ValueError("timeout binding is absent")
    if (
        set(receipt) != required
        or receipt.get("schema_version") != "job-supervision-timeout-v1"
        or receipt.get("failure_class") != "supervision-timeout"
        or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 124
        or read("exit_code") != b"124\n"
        or any((directory / name).exists() or (directory / name).is_symlink()
               for name in ("terminal-envelope.json", "terminal-envelope.sha256"))
    ):
        raise ValueError("invalid supervision timeout receipt")
    return receipt
