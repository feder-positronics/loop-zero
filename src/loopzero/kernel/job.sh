#!/usr/bin/env bash
# Run one long command detached and wait for it in a single blocking call.
#
# Agents burn wall-clock when they launch a multi-minute command with a short
# `yield_time_ms` and then poll the shell session every 30-60s: each poll is a
# full model round-trip, and ~40% of them return no output at all. This wrapper
# replaces the poll loop with: start (returns immediately) -> do other work ->
# wait (one blocking call that returns the exit code and the log tail).
#
# Usage:
#   scripts/util/job.sh start <name> [--run-id ID --task-id ID --terminal-artifact PATH] -- <command...>
#   scripts/util/job.sh wait  <name> [--timeout SECONDS] [--tail LINES] [--fast-cadence REASON]
#   scripts/util/job.sh run   <name> [--timeout SECONDS] [--tail LINES] -- <command...>
#   scripts/util/job.sh wait-file <path> [--timeout SECONDS] [--tail LINES] [--fast-cadence REASON]
#   scripts/util/job.sh status <name>
#   scripts/util/job.sh log   <name> [--tail LINES] [--follow]
#   scripts/util/job.sh list
#   scripts/util/job.sh binding-files --run-id <id>
#   scripts/util/job.sh reconcile <name> [--terminal-artifact <path>] [--primary <path>]
#   scripts/util/job.sh check                 # non-zero if any job is running or unreaped
#   scripts/util/job.sh clean [--all]
#
# `wait` exits with the job's own exit code, 124 on timeout, 2 on usage error.
# A bound final-CI reproduction `run` treats 124 as terminal: it stops and reaps
# the secure executor before releasing its held job-directory authority.
# `wait-file` blocks until <path> exists (a dispatched worker's terminal
# artifact is the wait event — #3418 P1); exit 0 on appearance, 124 on the
# deadman timeout, which means "worker presumed dead: run the orphan/lease
# check", never "poll again". Timeouts under 300s draw a warning unless
# --fast-cadence states why the watched state's own cadence is faster.
# Jobs live under $INTELFLO_JOB_DIR when explicitly configured. The default is
# a worktree-keyed directory under the OS account's durable state root, outside
# every delegated worktree. Legacy recovery uses INTELFLO_JOB_DIR explicitly.

set -uo pipefail

# The runner may cross a credential-bearing closeout boundary. Prevent Python
# startup hooks and the caller's working directory from becoming import roots.
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1

JOB_SCRIPT="$(realpath -m -- "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOB_WORKTREE="${INTELFLO_DELIVERY_ROOT:-$REPO_ROOT}"
PYTHON_BIN="/usr/bin/python3"
[ -x "$PYTHON_BIN" ] || {
	echo "job.sh: trusted Python interpreter is unavailable at $PYTHON_BIN" >&2
	exit 2
}
JOB_DIR="$("$PYTHON_BIN" "$REPO_ROOT/scripts/util/job_store.py" --worktree "$JOB_WORKTREE" --ensure-root)" || exit $?
JOB_ROOTS=("$JOB_DIR")
DEFAULT_TIMEOUT="${INTELFLO_JOB_TIMEOUT:-1800}"
DEFAULT_TAIL=40
RUN_AUTHORITY_FD=""
RUN_EXECUTOR_PID=""
RUN_INTERNAL_WAIT=0

usage() {
	sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
	exit "${1:-2}"
}

# Sub-300s timeouts rebuild the minute-poll shape wait-file exists to remove;
# demand a stated reason so genuine fast-cadence waits stay expressible.
FLOOR_SECONDS=300
warn_short_timeout() {
	local timeout="$1" fast_cadence="$2" verb="$3"
	if [ "$timeout" -lt "$FLOOR_SECONDS" ] && [ -z "$fast_cadence" ]; then
		echo "job.sh: warning: $verb --timeout ${timeout}s is below the ${FLOOR_SECONDS}s wait floor (ops.mdc);" >&2
		echo "  size the deadman from the p90, or state --fast-cadence '<why this state changes faster>'" >&2
	fi
}

die() {
	echo "job.sh: $*" >&2
	exit 2
}

# Job names become directory names; keep them predictable and path-safe.
validate_name() {
	local name="$1"
	[ -n "$name" ] || die "job name is required"
	case "$name" in
	*[!A-Za-z0-9._-]* | -* | .*) die "invalid job name '$name' (use [A-Za-z0-9._-], not starting with - or .)" ;;
	esac
}

job_path() {
	local name="$1" root found=""
	for root in "${JOB_ROOTS[@]}"; do
		if [ -e "$root/$name" ]; then
			[ -z "$found" ] || die "job '$name' exists in more than one authority root"
			found="$root/$name"
		fi
	done
	printf '%s\n' "${found:-$JOB_DIR/$name}"
}

executor_matches() {
	local dir="$1" name="$2" executor_pid="$3"
	[ "${INTELFLO_JOB_NAME:-}" = "$name" ] &&
		[ "${INTELFLO_JOB_EXECUTOR_PID:-}" = "$executor_pid" ] &&
		[ "$(cat "$dir/pid" 2>/dev/null || true)" = "$executor_pid" ]
}

job_lease_path() {
	local name="$1"
	"$PYTHON_BIN" "$REPO_ROOT/scripts/util/job_store.py" --worktree "$JOB_WORKTREE" --ensure-job-lease "$name"
}

# Open and acquire the stable per-name lease into the caller-named descriptor
# variable.  The Python lock operation acts on the shell's inherited open-file
# description, so the lock remains held after the helper exits.
acquire_job_lease() {
	local name="$1" output_variable="$2" lease_path held_fd
	lease_path="$(job_lease_path "$name")" || return $?
	exec {held_fd}<>"$lease_path" || return 1
	if ! "$PYTHON_BIN" - "$held_fd" "$lease_path" <<'PY'
import fcntl
import os
import sys
import time

descriptor = int(sys.argv[1])
path = sys.argv[2]
deadline = time.monotonic() + 0.1
while True:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(0.005)
    except OSError:
        raise SystemExit(1)
try:
    held = os.fstat(descriptor)
    current = os.stat(path, follow_symlinks=False)
except OSError:
    raise SystemExit(1)
if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
    raise SystemExit(1)
PY
	then
		eval "exec ${held_fd}>&-"
		return 1
	fi
	printf -v "$output_variable" '%s' "$held_fd"
}

close_job_lease() {
	local lease_fd="$1"
	[ -n "$lease_fd" ] || return 0
	eval "exec ${lease_fd}>&-"
}

write_waited_marker() {
	local dir="$1"
	"$PYTHON_BIN" - "$dir" <<'PY'
import os
import stat
import sys

directory_flags = os.O_RDONLY
if hasattr(os, "O_DIRECTORY"):
    directory_flags |= os.O_DIRECTORY
try:
    directory_fd = os.open(sys.argv[1], directory_flags)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    marker_fd = os.open("waited", flags, 0o600, dir_fd=directory_fd)
    marker = os.fstat(marker_fd)
    if not stat.S_ISREG(marker.st_mode) or marker.st_uid != os.getuid():
        raise OSError("invalid waited marker")
    os.fchmod(marker_fd, 0o600)
except OSError:
    raise SystemExit(1)
finally:
    if "marker_fd" in locals():
        os.close(marker_fd)
    if "directory_fd" in locals():
        os.close(directory_fd)
PY
}

job_reaped() {
	local dir="$1"
	if [ -f "$dir/binding.json" ] && [ ! -L "$dir/binding.json" ]; then
		[ -f "$dir/reconciliation.json" ] && [ ! -L "$dir/reconciliation.json" ] || return 1
		"$PYTHON_BIN" - "$dir/binding.json" "$dir/reconciliation.json" <<'PY' >/dev/null 2>&1
import hashlib
import json
import sys
from pathlib import Path

binding_path = Path(sys.argv[1])
try:
    binding_bytes = binding_path.read_bytes()
    binding = json.loads(binding_bytes)
    receipt = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(binding, dict) or not isinstance(receipt, dict):
    raise SystemExit(1)
for key in ("name", "run_id", "task_id"):
    if receipt.get(key) != binding.get(key):
        raise SystemExit(1)
schema = receipt.get("schema_version")
if schema in {"job-reconciliation-v1", "job-reconciliation-v2"}:
    raise SystemExit(0)
required = {
    "schema_version", "name", "run_id", "task_id", "failure_class",
    "reason", "binding_sha256",
}
if (
    schema != "job-supervisor-loss-v1"
    or set(receipt) != required
    or receipt.get("failure_class") != "supervisor-loss"
    or receipt.get("reason") not in {"startup-failure", "supervisor-loss"}
    or receipt.get("binding_sha256") != hashlib.sha256(binding_bytes).hexdigest()
):
    raise SystemExit(1)
PY
		return $?
	fi
	if [ -f "$dir/waited" ] && [ ! -L "$dir/waited" ]; then
		return 0
	fi
	return 1
}

terminate_job_lease_owner() {
	local pid_path="$1" identity_path="$2" lease_path="$3"
	"$PYTHON_BIN" - "$pid_path" "$identity_path" "$lease_path" <<'PY'
import json
import os
import re
import signal
import stat
import sys
from pathlib import Path

pid_path = Path(sys.argv[1])
identity_path = Path(sys.argv[2])
lease_path = Path(sys.argv[3])
try:
    pid_text = pid_path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[1-9][0-9]*", pid_text) is None:
        raise OSError("invalid pid")
    pid = int(pid_text)
    if identity_path.is_symlink():
        raise OSError("invalid pid identity")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    lease = lease_path.lstat()
    if lease_path.is_symlink() or not stat.S_ISREG(lease.st_mode):
        raise OSError("invalid lease")
    required = {
        "schema_version", "name", "pid", "starttime", "lease_dev", "lease_ino"
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != required
        or identity.get("schema_version") != "job-pid-identity-v1"
        or identity.get("name") != pid_path.parent.name
        or identity.get("pid") != pid
        or not isinstance(identity.get("starttime"), str)
        or re.fullmatch(r"[0-9]+", identity["starttime"]) is None
        or identity.get("lease_dev") != lease.st_dev
        or identity.get("lease_ino") != lease.st_ino
    ):
        raise OSError("pid identity does not match job authority")
    pidfd = os.pidfd_open(pid)
    stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rpartition(") ")[2].split()
    if len(stat_fields) < 20 or stat_fields[19] != identity["starttime"]:
        raise OSError("pid identity is stale")
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

try:
    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
except OSError:
    raise SystemExit(1)
finally:
    os.close(pidfd)
PY
}

# Advisory locks are usable only when an independent open-file description is
# excluded by the held lease.  Run this before publishing a v2 binding.
prove_job_lease_exclusion() {
	local lease_path="$1"
	"$PYTHON_BIN" - "$lease_path" <<'PY'
import fcntl
import os
import sys

flags = os.O_RDWR
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    descriptor = os.open(sys.argv[1], flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    finally:
        os.close(descriptor)
except OSError:
    raise SystemExit(2)
raise SystemExit(1)
PY
}

cmd_execute() {
	local dir="${1:-}" source_artifact="${2:-}" lease_fd="${3:-}"
	shift 3 || true
	[ "${1:-}" = "--" ] || die "internal executor expected '--' before the command"
	shift
	local name="${INTELFLO_JOB_NAME:-}"
	validate_name "$name"
	[ "$dir" = "$(job_path "$name")" ] || die "internal executor job path is invalid"
	[ -d "$dir" ] && [ ! -L "$dir" ] || die "internal executor job directory is invalid"
	# Replace this shell with the supervisor so the recorded PID is also the
	# process that holds every sealing descriptor. The wrapped command receives
	# no token, claim file, directory descriptor, or pending-artifact descriptor.
	exec "$PYTHON_BIN" - "$dir" "$source_artifact" "$name" "$REPO_ROOT" "$lease_fd" "$@" <<'PY'
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[4]).resolve() / "scripts" / "util"))
import job_store
from trusted_executable import TrustedExecutableError, system_executable

job_dir = Path(sys.argv[1])
source_path = Path(sys.argv[2])
job_name = sys.argv[3]
repo_root = Path(sys.argv[4]).resolve()
lease_fd = int(sys.argv[5])
command = sys.argv[6:]
FINAL_CI_REPRO_TASK_PREFIX = "final-ci-repro:"
FINAL_CI_REPRO_AUTH_FD_ENV = "INTELFLO_FINAL_CI_REPRO_AUTH_FD"
FINAL_CI_REPRO_AUTHORITY_ROOT_ENV = "INTELFLO_FINAL_CI_REPRO_PROTECTED_ROOT"
CODEX_AUTH_FD_ENV = "INTELFLO_CODEX_AUTH_FD"
WORKTREE_LEASE_FD_ENV = "INTELFLO_WORKTREE_LEASE_FD"
PROVIDER_SLOT_FD_ENV = "INTELFLO_PROVIDER_CONTINUATION_SLOT_FD"
TRUSTED_CONTINUATION_ENV = "INTELFLO_TRUSTED_CONTINUATION"

try:
    fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.fstat(lease_fd)
except (BlockingIOError, OSError) as exc:
    raise SystemExit(f"job lifecycle lease handoff is invalid: {exc}") from exc
# The supervisor retains the lease, but the wrapped child and every descendant
# must receive no descriptor capable of extending the lifecycle authority.
fcntl.fcntl(lease_fd, fcntl.F_SETFD, fcntl.FD_CLOEXEC)

directory_flags = os.O_RDONLY
if hasattr(os, "O_DIRECTORY"):
    directory_flags |= os.O_DIRECTORY
if hasattr(os, "O_NOFOLLOW"):
    directory_flags |= os.O_NOFOLLOW
directory_fd = os.open(job_dir, directory_flags)
authority_chain: list[tuple[Path, int]] = []
for authority_path in (job_dir.resolve(), *job_dir.resolve().parents):
    authority_chain.append(
        (authority_path, os.open(authority_path, directory_flags))
    )

def open_at(name: str, flags: int, mode: int = 0o400) -> int:
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, mode, dir_fd=directory_fd)

log_fd = open_at("log", os.O_WRONLY | os.O_APPEND)
log = os.fdopen(log_fd, "a", encoding="utf-8", buffering=1)

def report(message: str) -> None:
    print(f"job.sh: {message}", file=log, flush=True)

def read_held(name: str, *, wait: bool = False) -> tuple[int, bytes]:
    deadline = time.monotonic() + (2.0 if wait else 0.0)
    while True:
        try:
            descriptor = open_at(name, os.O_RDONLY)
            break
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        return descriptor, stream.read()

def held_path_matches(name: str, descriptor: int) -> bool:
    try:
        held = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)

def reserve(name: str) -> int:
    return open_at(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)

def write_all(descriptor: int, content: bytes) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]
    os.fsync(descriptor)

def commit_reserved(
    temporary: str, destination: str, descriptor: int, content: bytes
) -> None:
    write_all(descriptor, content)
    if not held_path_matches(temporary, descriptor):
        raise RuntimeError(f"parent-held {temporary} descriptor lost its pathname")
    os.link(
        temporary,
        destination,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    os.unlink(temporary, dir_fd=directory_fd)
    os.fsync(directory_fd)

def remove_at(name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass

def exists_at(name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True

def publish_exit_code(descriptor: int, code: int) -> None:
    content = f"{code}\n".encode("ascii")
    try:
        commit_reserved("exit_code.tmp", "exit_code", descriptor, content)
    except FileExistsError:
        # Only the executor that reserved every pending descriptor before the
        # wrapped command ran reaches here. A child-created early marker is
        # untrusted, so replace it once with this parent's fail-closed result.
        remove_at("exit_code")
        commit_reserved("exit_code.tmp", "exit_code", descriptor, content)

pid_fd = binding_fd = None
exit_fd = envelope_fd = digest_fd = None
final_code = 125
owns_launch = False
trusted_continuation = False
continuation_worktree_fd = None
continuation_provider_fd = None
continuation_digest = None
continuation_owner_path = None
terminal_artifact_sha256 = None
terminal_envelope_sha256 = None
terminal_status = None
try:
    pid_fd, pid_bytes = read_held("pid", wait=True)
    if int(pid_bytes.decode().strip()) != os.getpid():
        raise RuntimeError("executor PID does not match the launched job")

    try:
        binding_fd, binding_bytes = read_held("binding.json")
    except FileNotFoundError:
        binding_bytes = None
    binding = None
    if binding_bytes is not None:
        binding = json.loads(binding_bytes)
        required = {
            "schema_version", "name", "run_id", "task_id", "terminal_envelope"
        }
        if set(binding) != required or binding.get("schema_version") != "job-binding-v2":
            raise RuntimeError("job binding schema is invalid")
        if binding.get("name") != job_name or job_name != job_dir.name:
            raise RuntimeError("job binding name does not match its job directory")
        if not isinstance(binding.get("run_id"), str) or re.fullmatch(
            r"sr_[0-9a-f]{32}", binding["run_id"]
        ) is None:
            raise RuntimeError("job binding run_id is invalid")
        if not isinstance(binding.get("task_id"), str) or not binding["task_id"].strip():
            raise RuntimeError("job binding task_id is invalid")
        expected_envelope = (job_dir / "terminal-envelope.json").resolve()
        raw_envelope = binding.get("terminal_envelope")
        if not isinstance(raw_envelope, str) or Path(raw_envelope).resolve() != expected_envelope:
            raise RuntimeError("terminal envelope does not match the job binding")

    lease_identity = os.fstat(lease_fd)
    process_stat = Path(f"/proc/{os.getpid()}/stat").read_text(
        encoding="ascii"
    ).rpartition(") ")[2].split()
    if len(process_stat) < 20:
        raise RuntimeError("executor process identity is unreadable")
    pid_identity = {
        "schema_version": "job-pid-identity-v1",
        "name": job_name,
        "pid": os.getpid(),
        "starttime": process_stat[19],
        "lease_dev": lease_identity.st_dev,
        "lease_ino": lease_identity.st_ino,
    }
    pid_identity_fd = reserve("pid-identity.json")
    try:
        write_all(
            pid_identity_fd,
            (json.dumps(pid_identity, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.fsync(directory_fd)
    finally:
        os.close(pid_identity_fd)

    dispatcher = repo_root / "scripts" / "util" / "agent_dispatch.py"
    host_dispatcher = repo_root / "scripts" / "util" / "agent_dispatch_host.py"
    final_ci_gate = repo_root / "scripts" / "util" / "final_ci_gate.py"
    continuation_runner = repo_root / "scripts" / "util" / "delivery_pipeline.py"
    repro_connect_guard = repo_root / "scripts" / "util" / "repro_connect_guard.py"
    executable = shutil.which(command[0]) if command else None
    command_script = Path(command[1]) if len(command) > 1 else None

    def is_canonical_dispatch_result() -> bool:
        if binding is None or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", binding["task_id"]
        ) is None:
            return False
        expected = (
            repo_root / ".audit" / "dispatch" / "results"
            / f"{binding['task_id']}.json"
        )
        if not source_path.is_absolute() or source_path != expected:
            return False
        roots = (repo_root / ".audit", expected.parent.parent, expected.parent)
        try:
            if not all(
                path.is_dir()
                and not path.is_symlink()
                and path.stat(follow_symlinks=False).st_uid == os.getuid()
                and not path.stat(follow_symlinks=False).st_mode & stat.S_IWOTH
                for path in roots
            ):
                return False
            if expected.is_symlink():
                return False
            return not expected.exists() or expected.is_file()
        except OSError:
            return False

    trusted_dispatch_context = (
        binding is not None
        and len(command) >= 3
        and executable is not None
        and Path(executable).resolve() == Path(sys.executable).resolve()
        and command_script is not None
        and not command_script.is_symlink()
        and not job_dir.resolve().is_relative_to(repo_root)
        and (
            (
                source_path.is_relative_to(repo_root)
                and is_canonical_dispatch_result()
            )
            or (
                not source_path.is_relative_to(repo_root)
                and not source_path.resolve().is_relative_to(repo_root)
            )
        )
    )
    trusted_dispatcher = False
    if trusted_dispatch_context:
        assert command_script is not None
        if (
            not dispatcher.is_symlink()
            and dispatcher.is_file()
            and command_script.resolve() == dispatcher.resolve()
        ):
            trusted_dispatcher = command[2] == "run"
        elif (
            not host_dispatcher.is_symlink()
            and host_dispatcher.is_file()
            and command_script.resolve() == host_dispatcher.resolve()
        ):
            host_arguments = command[2:]
            trusted_dispatcher = host_arguments[0] == "run"
            if host_arguments[0] == "--repo":
                trusted_dispatcher = (
                    len(host_arguments) >= 3
                    and bool(host_arguments[1])
                    and not host_arguments[1].startswith("-")
                    and host_arguments[2] == "run"
                )
            elif host_arguments[0].startswith("--repo="):
                trusted_dispatcher = (
                    len(host_arguments) >= 2
                    and bool(host_arguments[0].partition("=")[2])
                    and host_arguments[1] == "run"
                )

    if (
        binding is not None
        and len(command) == 9
        and executable is not None
        and Path(executable).resolve() == Path(sys.executable).resolve()
        and command_script is not None
        and not continuation_runner.is_symlink()
        and continuation_runner.is_file()
        and not command_script.is_symlink()
        and command_script.resolve() == continuation_runner.resolve()
        and command[2] == "continue"
        and command[3] == "--candidate-file"
        and command[5] == "--candidate-sha256"
        and re.fullmatch(r"[0-9a-f]{64}", command[6]) is not None
        and command[7] == "--terminal-artifact"
    ):
        continuation_digest = command[6]
        candidate_path = Path(command[4])
        expected_candidate = (
            repo_root / ".audit" / "delivery-continuations" / "candidates"
            / f"{continuation_digest}.json"
        )
        expected_result = (
            repo_root / ".audit" / "delivery-continuations" / "results"
            / f"{continuation_digest}.json"
        )
        continuation_roots = (
            repo_root / ".audit",
            repo_root / ".audit" / "delivery-continuations",
            expected_candidate.parent,
            expected_result.parent,
        )
        if (
            candidate_path.is_absolute()
            and candidate_path == expected_candidate
            and source_path == expected_result
            and Path(command[8]) == expected_result
            and candidate_path.is_file()
            and not candidate_path.is_symlink()
            and all(
                path.is_dir()
                and not path.is_symlink()
                and path.stat(follow_symlinks=False).st_uid == os.getuid()
                and not path.stat(follow_symlinks=False).st_mode & stat.S_IWOTH
                for path in continuation_roots
            )
            and binding["name"]
            == f"delivery-continuation-{continuation_digest[:20]}"
            and binding["task_id"] == f"continuation-{continuation_digest[:24]}"
        ):
            candidate_bytes = candidate_path.read_bytes()
            candidate = json.loads(candidate_bytes)
            raw_worktree_fd = os.environ.get(WORKTREE_LEASE_FD_ENV)
            raw_provider_fd = os.environ.get(PROVIDER_SLOT_FD_ENV)
            try:
                continuation_worktree_fd = int(raw_worktree_fd or "")
                held_worktree = os.fstat(continuation_worktree_fd)
                fcntl.flock(
                    continuation_worktree_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                git_dir = Path(
                    subprocess.run(
                        [
                            "git", "-C", os.getcwd(), "rev-parse",
                            "--path-format=absolute", "--git-dir",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout.strip()
                ).resolve()
                worktree_lock = git_dir / "worktree-boundary.lock"
                continuation_owner_path = git_dir / "worktree-boundary.owner.json"
                current_worktree = worktree_lock.stat(follow_symlinks=False)
            except (OSError, ValueError, subprocess.SubprocessError):
                continuation_worktree_fd = None
            worktree_fd_valid = (
                continuation_worktree_fd is not None
                and (held_worktree.st_dev, held_worktree.st_ino)
                == (current_worktree.st_dev, current_worktree.st_ino)
            )
            provider_required = candidate.get("provider_slot_required") is True
            provider_fd_valid = not provider_required and raw_provider_fd is None
            if provider_required and raw_provider_fd is not None:
                try:
                    continuation_provider_fd = int(raw_provider_fd)
                    held_provider = os.fstat(continuation_provider_fd)
                    fcntl.flock(
                        continuation_provider_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    common_dir = Path(
                        subprocess.run(
                            [
                                "git", "-C", os.getcwd(), "rev-parse",
                                "--path-format=absolute", "--git-common-dir",
                            ],
                            capture_output=True,
                            text=True,
                            check=True,
                        ).stdout.strip()
                    ).resolve()
                    provider_paths = [
                        common_dir / "delivery-provider-continuation-slots"
                        / f"slot-{index}.lock"
                        for index in range(6)
                    ]
                    provider_fd_valid = any(
                        path.is_file()
                        and not path.is_symlink()
                        and (held_provider.st_dev, held_provider.st_ino)
                        == (
                            path.stat(follow_symlinks=False).st_dev,
                            path.stat(follow_symlinks=False).st_ino,
                        )
                        for path in provider_paths
                    )
                except (BlockingIOError, OSError, ValueError, subprocess.SubprocessError):
                    continuation_provider_fd = None
                    provider_fd_valid = False
            trusted_continuation = bool(
                isinstance(candidate, dict)
                and set(candidate)
                == {
                    "schema_version", "run_id", "branch", "pr", "worktree",
                    "issue", "skill", "head_sha", "head_tree_sha",
                    "risk_digest", "risk_json", "risk_file_path", "risk_tier",
                    "pr_body", "pr_body_path", "pr_body_digest", "pr_title",
                    "pr_labels", "origin_url", "git_config_sha256",
                    "authority_repo", "review_task_id", "review_task_path",
                    "review_task_digest", "review_preflight_path",
                    "review_preflight_digest", "predecessor_findings_digest",
                    "previous_repair_terminal_digest", "required_evidence",
                    "required_evidence_digest", "provider_slot_required",
                }
                and hashlib.sha256(
                    json.dumps(
                        candidate,
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                == continuation_digest
                and candidate.get("schema_version")
                == "delivery-continuation-candidate-v1"
                and candidate.get("authority_repo") == str(repo_root)
                and candidate.get("worktree") == str(Path.cwd().resolve())
                and candidate.get("run_id") == binding["run_id"]
                and isinstance(candidate.get("pr"), int)
                and not isinstance(candidate.get("pr"), bool)
                and candidate["pr"] > 0
                and isinstance(candidate.get("issue"), int)
                and not isinstance(candidate.get("issue"), bool)
                and candidate["issue"] > 0
                and candidate.get("skill") in {"execute-blueprint", "work-issue"}
                and isinstance(candidate.get("branch"), str)
                and bool(candidate["branch"])
                and re.fullmatch(r"[0-9a-f]{40}", str(candidate.get("head_sha") or ""))
                is not None
                and re.fullmatch(
                    r"[0-9a-f]{40}", str(candidate.get("head_tree_sha") or "")
                )
                is not None
                and candidate.get("risk_tier") in {"T0", "T1", "T2"}
                and (
                    (not provider_required and candidate.get("risk_tier") == "T0")
                    or (
                        provider_required
                        and candidate.get("risk_tier") in {"T1", "T2"}
                        and isinstance(candidate.get("review_task_id"), str)
                        and bool(candidate["review_task_id"])
                    )
                )
                and worktree_fd_valid
                and provider_fd_valid
            )

    repro_signature = None
    if (
        binding is not None
        and len(command) == 10
        and executable is not None
        and Path(executable).resolve() == Path(sys.executable).resolve()
        and not final_ci_gate.is_symlink()
        and final_ci_gate.is_file()
        and command_script is not None
        and command_script.resolve() == final_ci_gate.resolve()
        and command[2] == "--pr"
        and re.fullmatch(r"[1-9][0-9]*", command[3]) is not None
        and command[4] == "--repo"
        and Path(command[5]).resolve() == repo_root
        and command[6] == "--execute-repro"
        and re.fullmatch(r"[0-9a-f]{64}", command[7]) is not None
        and command[8] == "--terminal-artifact"
        and Path(command[9]).resolve() == source_path.resolve()
        and binding["task_id"]
        == f"{FINAL_CI_REPRO_TASK_PREFIX}{command[7]}"
    ):
        repro_signature = command[7]
    final_ci_repro_task = (
        binding is not None
        and binding["task_id"].startswith(FINAL_CI_REPRO_TASK_PREFIX)
    )
    if final_ci_repro_task and repro_signature is None:
        raise RuntimeError(
            "bound final-CI reproduction requires the canonical executor command"
        )
    bwrap = None
    if (
        binding is not None
        and not trusted_dispatcher
        and not trusted_continuation
        and repro_signature is None
    ):
        try:
            bwrap = str(system_executable("bwrap"))
        except TrustedExecutableError as exc:
            raise RuntimeError(
                "bound job supervision requires protected bubblewrap"
            ) from exc
        capability_probe = subprocess.run(
            [bwrap, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        capability_surface = capability_probe.stdout + capability_probe.stderr
        if (
            "--perms" not in capability_surface
            or "--remount-ro" not in capability_surface
        ):
            raise RuntimeError(
                "bound job supervision requires bubblewrap with --perms and "
                "--remount-ro support"
            )

    durable_names = ["exit_code"]
    if binding is not None:
        durable_names.extend(("terminal-envelope.json", "terminal-envelope.sha256"))
    if any(exists_at(name) for name in durable_names):
        raise RuntimeError("durable terminal destination already exists")

    exit_fd = reserve("exit_code.tmp")
    if binding is not None:
        envelope_fd = reserve("terminal-envelope.json.tmp")
        digest_fd = reserve("terminal-envelope.sha256.tmp")
    owns_launch = True

    child_env = dict(os.environ)
    child_env.pop("INTELFLO_JOB_TOKEN", None)
    child_env.pop(FINAL_CI_REPRO_AUTH_FD_ENV, None)
    if CODEX_AUTH_FD_ENV in child_env:
        # close_fds invalidates the inherited descriptor. Preserve an explicit
        # unusable capability so the host wrapper cannot mistake its absence
        # for permission to reopen owner credential state.
        child_env[CODEX_AUTH_FD_ENV] = "-1"
    # The supervisor's trusted Python helpers need safe-path mode, but a
    # wrapped Python script must retain its own directory as an import root.
    child_env.pop("PYTHONSAFEPATH", None)
    child_env["INTELFLO_JOB_NAME"] = job_name
    child_env["INTELFLO_JOB_EXECUTOR_PID"] = str(os.getpid())
    if trusted_continuation:
        child_env[TRUSTED_CONTINUATION_ENV] = "1"
    wrapped_command = command
    repro_authorization_key = None
    repro_lifetime_token = None
    repro_lifetime_listener = None
    repro_lifetime_connections = []
    repro_lifetime_thread = None
    repro_lifetime_stopping = threading.Event()

    def stop_repro_service(_signum, _frame) -> None:
        repro_lifetime_stopping.set()
        if repro_lifetime_listener is not None:
            repro_lifetime_listener.close()
        for connection in repro_lifetime_connections:
            connection.close()

    if repro_signature is not None:
        repro_authorization_key = os.urandom(32)
        repro_lifetime_token = os.urandom(32)
        repro_lifetime_name = f"@intelflo-repro-{os.urandom(16).hex()}"
        repro_lifetime_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        repro_lifetime_listener.bind("\0" + repro_lifetime_name[1:])
        repro_lifetime_listener.listen(1)

        def accept_repro_lifetime() -> None:
            try:
                connection, _ = repro_lifetime_listener.accept()
                supplied = b""
                while len(supplied) < len(repro_lifetime_token):
                    chunk = connection.recv(len(repro_lifetime_token) - len(supplied))
                    if not chunk:
                        break
                    supplied += chunk
                if not hmac.compare_digest(supplied, repro_lifetime_token):
                    connection.close()
                    return
                repro_lifetime_connections.append(connection)
                if repro_lifetime_stopping.is_set():
                    connection.close()
            except OSError:
                return

        repro_lifetime_thread = threading.Thread(
            target=accept_repro_lifetime,
            name="repro-lifetime-authority",
            daemon=True,
        )
        repro_lifetime_thread.start()
        signal.signal(signal.SIGTERM, stop_repro_service)
        try:
            systemd_run = str(system_executable("systemd-run"))
        except TrustedExecutableError as exc:
            raise RuntimeError(
                "bound final-CI reproduction requires protected systemd-run"
            ) from exc
        protected_authority_root = job_dir.resolve().parent
        if repro_connect_guard.is_symlink() or not repro_connect_guard.is_file():
            raise RuntimeError("bound final-CI reproduction connect guard is unavailable")
        service_environment = [
            f"--setenv={name}"
            for name in sorted(child_env)
            if name not in {
                "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR",
                "INTELFLO_JOB_EXECUTOR_PID",
            }
        ]
        wrapped_command = [
            systemd_run,
            "--user",
            "--wait",
            "--collect",
            "--quiet",
            "--pipe",
            "--expand-environment=no",
            "--service-type=exec",
            "--property=KillMode=control-group",
            f"--working-directory={os.getcwd()}",
            *service_environment,
            "--setenv=DBUS_SESSION_BUS_ADDRESS=",
            "--setenv=XDG_RUNTIME_DIR=",
            f"--setenv={FINAL_CI_REPRO_AUTH_FD_ENV}=0",
            f"--setenv={FINAL_CI_REPRO_AUTHORITY_ROOT_ENV}={protected_authority_root}",
            str(sys.executable),
            str(repro_connect_guard),
            "--lifetime-socket",
            repro_lifetime_name,
            "--",
            *command,
        ]
    if (
        binding is not None
        and not trusted_dispatcher
        and not trusted_continuation
        and repro_signature is None
    ):
        assert bwrap is not None
        # The synthesized-root rationale and invariants live on
        # job_store.build_bound_sandbox_arguments. The parent keeps the only
        # writable authority descriptors, and the private PID namespace plus
        # --die-with-parent removes delayed peers. job_store.py resolves
        # exactly one authority root. Legacy recovery is explicit, so the
        # fallback never leaves a second discovered root writable.
        try:
            wrapped_command = job_store.build_bound_sandbox_arguments(
                bwrap,
                protected_authority_root=job_dir.resolve().parent,
                working_directory=Path(os.getcwd()),
                command=list(command),
            )
        except job_store.JobStoreError as exc:
            raise RuntimeError(str(exc)) from exc
    run_arguments = {
        "check": False,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": child_env,
        "close_fds": True,
    }
    if repro_authorization_key is None:
        run_arguments["stdin"] = subprocess.DEVNULL
    else:
        run_arguments["input"] = repro_authorization_key + repro_lifetime_token
    if trusted_continuation:
        inherited = [continuation_worktree_fd]
        if continuation_provider_fd is not None:
            inherited.append(continuation_provider_fd)
        run_arguments["pass_fds"] = tuple(inherited)
    # A same-account child must not be able to reopen the supervisor's held
    # authority or sealing descriptors through /proc.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    try:
        completed = subprocess.run(wrapped_command, **run_arguments)
    finally:
        if repro_lifetime_listener is not None:
            repro_lifetime_listener.close()
        for connection in repro_lifetime_connections:
            connection.close()
        if repro_lifetime_thread is not None:
            repro_lifetime_thread.join(timeout=1)
    final_code = completed.returncode

    for authority_path, authority_fd in authority_chain:
        held = os.fstat(authority_fd)
        current = os.stat(authority_path, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError(
                "durable job authority path changed during execution"
            )

    if not held_path_matches("pid", pid_fd):
        raise RuntimeError("launched job PID authority changed during execution")
    os.lseek(pid_fd, 0, os.SEEK_SET)
    if os.read(pid_fd, 128) != pid_bytes:
        raise RuntimeError("launched job PID authority changed during execution")
    if binding_fd is not None:
        if not held_path_matches("binding.json", binding_fd):
            raise RuntimeError("job binding changed during execution")
        os.lseek(binding_fd, 0, os.SEEK_SET)
        if os.read(binding_fd, len(binding_bytes) + 1) != binding_bytes:
            raise RuntimeError("job binding changed during execution")

    if binding is not None:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        try:
            source_fd = os.open(source_path, source_flags)
            try:
                with os.fdopen(os.dup(source_fd), "rb") as stream:
                    artifact_bytes = stream.read()
            finally:
                os.close(source_fd)
            terminal = json.loads(artifact_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"authoritative terminal artifact is unreadable: {exc}"
            ) from exc
        if repro_authorization_key is not None:
            if not isinstance(terminal, dict):
                raise RuntimeError(
                    "final-CI reproduction terminal authorization is invalid"
                )
            authorization = terminal.get("job_authorization_hmac_sha256")
            unsigned_terminal = dict(terminal)
            unsigned_terminal.pop("job_authorization_hmac_sha256", None)
            expected_authorization = hmac.new(
                repro_authorization_key,
                json.dumps(
                    unsigned_terminal,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode(),
                hashlib.sha256,
            ).hexdigest()
            if not isinstance(authorization, str) or not hmac.compare_digest(
                authorization, expected_authorization
            ):
                raise RuntimeError(
                    "final-CI reproduction terminal authorization is invalid"
                )
        if not isinstance(terminal, dict) or terminal.get("task_id") != binding["task_id"]:
            raise RuntimeError("terminal artifact task_id does not match the job binding")
        status = terminal.get("status")
        terminal_statuses = {
            "completed", "needs-escalation", "scope-violation",
            "acceptance-failure", "infrastructure-failure",
            "packaging-failure", "failed",
        }
        if status == "blocked":
            if final_code != 0:
                raise RuntimeError("blocked terminal status conflicts with the wrapper exit code")
        elif status == "repair-required":
            if not trusted_continuation or final_code != 3:
                raise RuntimeError(
                    "repair-required terminal status conflicts with the wrapper exit code"
                )
        elif status not in terminal_statuses:
            raise RuntimeError("terminal artifact status is not authoritative")
        elif (status == "completed") != (final_code == 0):
            raise RuntimeError("terminal artifact status conflicts with the wrapper exit code")
        terminal_artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        terminal_status = status
        envelope = {
            "schema_version": "job-terminal-envelope-v2",
            "name": binding["name"],
            "run_id": binding["run_id"],
            "task_id": binding["task_id"],
            "source_terminal_artifact": str(source_path.resolve()),
            "terminal_artifact_sha256": terminal_artifact_sha256,
            "command_exit_code": final_code,
            "terminal": terminal,
        }
        envelope_bytes = (
            json.dumps(envelope, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest_bytes = (
            hashlib.sha256(envelope_bytes).hexdigest() + "\n"
        ).encode("ascii")
        terminal_envelope_sha256 = digest_bytes.decode("ascii").strip()
        commit_reserved(
            "terminal-envelope.json.tmp",
            "terminal-envelope.json",
            envelope_fd,
            envelope_bytes,
        )
        try:
            commit_reserved(
                "terminal-envelope.sha256.tmp",
                "terminal-envelope.sha256",
                digest_fd,
                digest_bytes,
            )
        except BaseException:
            remove_at("terminal-envelope.json")
            raise
except BaseException as exc:
    report(f"terminal supervision failed: {exc}")
    final_code = 125
    if owns_launch:
        remove_at("terminal-envelope.json")
        remove_at("terminal-envelope.sha256")
finally:
    if owns_launch:
        try:
            publish_exit_code(exit_fd, final_code)
        except BaseException as exc:
            report(f"terminal exit code could not be sealed: {exc}")
            final_code = 125
    continuation_reaped = False
    if (
        owns_launch
        and trusted_continuation
        and terminal_artifact_sha256 is not None
        and terminal_envelope_sha256 is not None
        and terminal_status is not None
        and binding is not None
    ):
        reconciliation_fd = waited_fd = None
        try:
            reconciliation = {
                "schema_version": "job-reconciliation-v2",
                "name": binding["name"],
                "run_id": binding["run_id"],
                "task_id": binding["task_id"],
                "exit_code": final_code,
                "terminal_status": terminal_status,
                "terminal_artifact_sha256": terminal_artifact_sha256,
                "terminal_envelope": str(
                    (job_dir / "terminal-envelope.json").resolve()
                ),
                "terminal_envelope_sha256": terminal_envelope_sha256,
                "source_terminal_artifact": str(source_path.resolve()),
            }
            reconciliation_fd = reserve("reconciliation.json.tmp")
            commit_reserved(
                "reconciliation.json.tmp",
                "reconciliation.json",
                reconciliation_fd,
                (json.dumps(reconciliation, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
            waited_fd = reserve("waited.tmp")
            commit_reserved("waited.tmp", "waited", waited_fd, b"")
            continuation_reaped = True
        except BaseException as exc:
            report(f"trusted continuation could not self-reconcile: {exc}")
        finally:
            for descriptor in (reconciliation_fd, waited_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
    if trusted_continuation and continuation_owner_path is not None:
        nonce = os.environ.get("INTELFLO_WORKTREE_LEASE_NONCE")
        if nonce:
            try:
                owner = json.loads(continuation_owner_path.read_text(encoding="utf-8"))
                if owner.get("nonce_sha256") == hashlib.sha256(nonce.encode()).hexdigest():
                    continuation_owner_path.unlink()
            except (OSError, json.JSONDecodeError):
                pass
    for descriptor in (continuation_provider_fd, continuation_worktree_fd):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if continuation_reaped:
        try:
            refill_home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            refill_home = ""
        refill_env = {
            "HOME": refill_home,
            "LANG": child_env.get("LANG", "C.UTF-8"),
            "LC_ALL": child_env.get("LC_ALL", "C.UTF-8"),
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
        }
        if not Path(refill_home).is_absolute() or not Path(refill_home).is_dir():
            report("bounded continuation refill has no valid account home")
            refill = None
        else:
            refill = subprocess.run(
                [
                    str(sys.executable),
                    str(continuation_runner),
                    "fill",
                    "--authority-repo",
                    str(repo_root),
                    "--limit",
                    "1",
                ],
                cwd=repo_root,
                env=refill_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if refill is not None and refill.returncode != 0:
            report(f"bounded continuation refill failed with exit {refill.returncode}")
    for descriptor in (pid_fd, binding_fd, exit_fd, envelope_fd, digest_fd, lease_fd):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    for _, descriptor in authority_chain:
        try:
            os.close(descriptor)
        except OSError:
            pass
    os.close(directory_fd)
    log.close()

raise SystemExit(final_code)
PY
}

record_retryable_continuation_interruption() {
	local dir="$1" name="$2" run_id="$3" task_id="$4" terminal_artifact="$5"
	shift 5
	"$PYTHON_BIN" - "$REPO_ROOT" "$dir" "$name" "$run_id" "$task_id" "$terminal_artifact" "$@" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
job_dir = Path(sys.argv[2]).resolve()
name, run_id, task_id = sys.argv[3:6]
terminal = Path(sys.argv[6])
command = sys.argv[7:]
runner = repo / "scripts" / "util" / "delivery_pipeline.py"
if len(command) != 9:
    raise SystemExit(1)
executable = Path(command[0]).resolve()
digest = command[6]
candidate = Path(command[4])
expected_candidate = (
    repo / ".audit" / "delivery-continuations" / "candidates" / f"{digest}.json"
)
expected_terminal = (
    repo / ".audit" / "delivery-continuations" / "results" / f"{digest}.json"
)
if not (
    executable == Path("/usr/bin/python3").resolve()
    and Path(command[1]).resolve() == runner.resolve()
    and not Path(command[1]).is_symlink()
    and command[2:4] == ["continue", "--candidate-file"]
    and command[5] == "--candidate-sha256"
    and re.fullmatch(r"[0-9a-f]{64}", digest)
    and command[7] == "--terminal-artifact"
    and Path(command[8]).resolve() == terminal == expected_terminal
    and candidate == expected_candidate
    and candidate.is_absolute()
    and name == f"delivery-continuation-{digest[:20]}"
    and task_id == f"continuation-{digest[:24]}"
    and candidate.is_file()
    and not candidate.is_symlink()
    and not os.path.lexists(expected_terminal)
    and not (job_dir / "terminal-envelope.json").exists()
    and not (job_dir / "reconciliation.json").exists()
):
    raise SystemExit(1)
try:
    binding_bytes = (job_dir / "binding.json").read_bytes()
    binding = json.loads(binding_bytes)
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if binding.get("name") != name or binding.get("run_id") != run_id or binding.get("task_id") != task_id:
    raise SystemExit(1)
log_path = job_dir / "log"
log_bytes = log_path.read_bytes() if log_path.is_file() and not log_path.is_symlink() else b""
receipt = {
    "schema_version": "delivery-continuation-interruption-v1",
    "candidate_digest": digest,
    "name": name,
    "run_id": run_id,
    "task_id": task_id,
    "binding_sha256": hashlib.sha256(binding_bytes).hexdigest(),
    "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
}
exit_path = job_dir / "exit_code"
if exit_path.is_file() and not exit_path.is_symlink():
    raw_exit = exit_path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9]+", raw_exit):
        receipt["previous_exit_code"] = int(raw_exit)
encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
receipt_sha256 = hashlib.sha256(encoded).hexdigest()
interruption_dir = repo / ".audit" / "delivery-continuations" / "interruptions"
interruption_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
for directory in (repo / ".audit", interruption_dir.parent, interruption_dir):
    metadata = os.lstat(directory)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & stat.S_IWOTH
    ):
        raise SystemExit(1)
out = interruption_dir / f"{digest}-{receipt['binding_sha256']}-{receipt_sha256}.json"
prior_receipts = [
    path
    for path in interruption_dir.glob(f"{digest}-*.json")
    if path.is_file() and not path.is_symlink()
]
if os.path.lexists(out) or len(prior_receipts) >= 3:
    raise SystemExit(1)
try:
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
except FileExistsError:
    raise SystemExit(1)
else:
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
PY
}

cmd_start() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	local run_id="" task_id="" terminal_artifact=""
	while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
		case "$1" in
		--run-id | --task-id | --terminal-artifact)
			local option="$1"
			[ "$#" -ge 2 ] || die "$option requires a value"
			case "$option" in
			--run-id) run_id="$2" ;;
			--task-id) task_id="$2" ;;
			--terminal-artifact) terminal_artifact="$2" ;;
			esac
			shift 2
			;;
		*) die "unknown start option '$1'" ;;
		esac
	done
	[ "${1:-}" = "--" ] || die "expected '--' before the command"
	shift
	[ "$#" -gt 0 ] || die "no command given"
	if [ -n "$run_id$task_id$terminal_artifact" ]; then
		[[ "$run_id" =~ ^sr_[0-9a-f]{32}$ ]] || die "bound job --run-id must match sr_<32 lowercase hex>"
		[ -n "$task_id" ] && [ -n "$terminal_artifact" ] || die "bound jobs require --run-id, --task-id, and --terminal-artifact together"
		# Preserve the lexical path so the executor can reject symlink traversal
		# for the one writable in-repo dispatcher result exception.
		terminal_artifact="$(realpath -ms -- "$terminal_artifact")"
		local resolved_terminal_artifact
		resolved_terminal_artifact="$(realpath -m -- "$terminal_artifact")"
		if [[ "$terminal_artifact" != "$REPO_ROOT/"* && "$resolved_terminal_artifact" != "$REPO_ROOT/"* ]]; then
			# Preserve existing external symlink compatibility without laundering
			# an internal result through an external target (or vice versa).
			terminal_artifact="$resolved_terminal_artifact"
		fi
	fi

	local dir lease_fd="" lease_path
	dir="$(job_path "$name")" || return $?
	if ! acquire_job_lease "$name" lease_fd; then
		die "job '$name' is already running under an active launcher or supervisor; use 'wait' or pick another name"
	fi
	lease_path="$(job_lease_path "$name")" || die "cannot resolve stable job lease"
	if ! prove_job_lease_exclusion "$lease_path"; then
		die "job '$name' cannot prove independent lifecycle-lock exclusion"
	fi
	if [ -d "$dir" ] && ! job_reaped "$dir" &&
		{ [ -f "$dir/binding.json" ] || [ -f "$dir/pid" ] || [ -f "$dir/exit_code" ]; }; then
		if ! record_retryable_continuation_interruption \
			"$dir" "$name" "$run_id" "$task_id" "$terminal_artifact" "$@"; then
			die "job '$name' has an unreaped result; use 'wait' or 'reconcile' before reusing its name"
		fi
	fi
	rm -rf "$dir"
	local created_dir
	created_dir="$("$PYTHON_BIN" "$REPO_ROOT/scripts/util/job_store.py" --worktree "$JOB_WORKTREE" --ensure-job-directory "$name")" ||
		die "cannot create protected job directory"
	[ "$created_dir" = "$dir" ] || die "job authority path changed during creation"

	# Record the command before launching so `status` is meaningful even if the
	# process dies immediately.
	printf '%s\n' "$*" >"$dir/cmd"
	date -u '+%Y-%m-%dT%H:%M:%SZ' >"$dir/started_at"
	: >"$dir/log"
	if [ -n "$run_id" ]; then
		"$PYTHON_BIN" - "$dir/binding.json" "$name" "$run_id" "$task_id" "$dir/terminal-envelope.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": "job-binding-v2",
    "name": sys.argv[2],
    "run_id": sys.argv[3],
    "task_id": sys.argv[4],
    "terminal_envelope": str(Path(sys.argv[5]).resolve()),
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
		if [ "$?" -ne 0 ]; then
			rm -rf "$dir"
			die "could not persist delivery-run job binding"
		fi
	fi
	if [ "${HOLD_AUTHORITY_FOR_RUN:-0}" = "1" ]; then
		exec {RUN_AUTHORITY_FD}<"$dir" || die "cannot hold job authority for run"
	fi

	# setsid detaches from the agent's shell session, so the job survives the
	# exec_command timeout that would otherwise orphan or kill it.
	setsid env INTELFLO_JOB_NAME="$name" \
		"$JOB_SCRIPT" _execute "$dir" "$terminal_artifact" "$lease_fd" -- "$@" \
		</dev/null >/dev/null 2>&1 &
	local pid=$!
	RUN_EXECUTOR_PID="$pid"
	printf '%s\n' "$pid" >"$dir/pid"
	disown "$pid" 2>/dev/null || true
	close_job_lease "$lease_fd"

	echo "started job '$name' (pid $pid)"
	echo "  log:  $dir/log"
	echo "  wait: scripts/util/job.sh wait $name --timeout <seconds>"
	echo "  note: ONE blocking wait sized from p90; under a harness foreground cap run the wait as a background task (completion notifies) — never drain stdin at short intervals"
}

job_running() {
	local dir="$1"
	local name lease_path lease_fd=""
	name="$(basename "$dir")"
	lease_path="$(dirname "$dir")/.leases/$name.lock"
	# A read-only probe must neither create the lifecycle authority nor take its
	# exclusive owner lock.  Shared acquisition succeeds only when no launcher or
	# supervisor holds the exclusive lease.  Lifecycle writers tolerate this
	# sub-millisecond reader through acquire_job_lease's bounded retry.
	if [ ! -e "$lease_path" ]; then
		# Pre-lease state from an upgrade or external authority loss is ambiguous
		# when durable live-job evidence exists, so status fails closed as active.
		{ [ -f "$dir/pid" ] && [ ! -L "$dir/pid" ]; } ||
			{ [ -f "$dir/binding.json" ] && [ ! -L "$dir/binding.json" ]; } || return 1
		return 0
	fi
	[ -f "$lease_path" ] && [ ! -L "$lease_path" ] && [ -O "$lease_path" ] || return 0
	exec {lease_fd}<>"$lease_path" || return 0
	if "$PYTHON_BIN" - "$lease_fd" <<'PY'
import fcntl
import sys

descriptor = int(sys.argv[1])
try:
    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
except (BlockingIOError, OSError):
    raise SystemExit(1)
fcntl.flock(descriptor, fcntl.LOCK_UN)
PY
	then
		close_job_lease "$lease_fd"
		return 1
	fi
	close_job_lease "$lease_fd"
	return 0
}

missing_lease_terminal_hint() {
	local dir="$1" name="$2" lease_path
	lease_path="$(dirname "$dir")/.leases/$name.lock"
	[ ! -e "$lease_path" ] || return 1
	{ [ -f "$dir/exit_code" ] && [ ! -L "$dir/exit_code" ]; } ||
		{ [ -f "$dir/waited" ] && [ ! -L "$dir/waited" ]; } || return 1
	if [ -f "$dir/binding.json" ] && [ ! -L "$dir/binding.json" ]; then
		echo "lifecycle lease is missing despite terminal evidence; run 'reconcile $name'"
	else
		echo "lifecycle lease is missing despite terminal evidence; inspect the job, then clean --all if the result is not needed"
	fi
}

emit_result() {
	local dir="$1" name="$2" tail_lines="$3" code="$4" display_dir="${5:-$1}"
	echo "job '$name' finished with exit code $code"
	echo "--- last $tail_lines log lines ---"
	tail -n "$tail_lines" "$dir/log" 2>/dev/null || true
	echo "--- end of log ($(wc -l <"$dir/log" 2>/dev/null || echo 0) lines total, full log: $display_dir/log) ---"
}

cmd_wait() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	local timeout="$DEFAULT_TIMEOUT" tail_lines="$DEFAULT_TAIL" fast_cadence=""
	local authority_fd="" expected_pid=""
	while [ "$#" -gt 0 ]; do
		case "$1" in
		--timeout)
			timeout="${2:-}"
			shift 2
			;;
		--tail)
			tail_lines="${2:-}"
			shift 2
			;;
		--fast-cadence)
			fast_cadence="${2:-}"
			shift 2
			;;
		--authority-fd)
			authority_fd="${2:-}"
			shift 2
			;;
		--expected-pid)
			expected_pid="${2:-}"
			shift 2
			;;
		*) die "unknown option '$1'" ;;
		esac
	done
	case "$timeout" in '' | *[!0-9]*) die "--timeout must be a whole number of seconds" ;; esac
	case "$tail_lines" in '' | *[!0-9]*) die "--tail must be a whole number of lines" ;; esac
	warn_short_timeout "$timeout" "$fast_cadence" "wait"

	local dir waited_generation="" observed_job_fd="" display_dir
	if [ -n "$authority_fd" ] || [ -n "$expected_pid" ]; then
		[[ "$authority_fd" =~ ^[0-9]+$ ]] || die "internal authority fd is invalid"
		[[ "$expected_pid" =~ ^[1-9][0-9]*$ ]] || die "internal executor pid is invalid"
		# These arguments are a same-shell capability passed only by cmd_run;
		# public callers cannot mint directory authority by naming an open FD.
		[ "$RUN_INTERNAL_WAIT" -eq 1 ] &&
			[ "$authority_fd" = "$RUN_AUTHORITY_FD" ] &&
			[ "$expected_pid" = "$RUN_EXECUTOR_PID" ] ||
			die "internal wait authority is unavailable"
		dir="/proc/$$/fd/$authority_fd"
	else
		dir="$(job_path "$name")" || return $?
	fi
	[ -d "$dir" ] || die "no such job '$name' (try 'job.sh list')"
	if [ -n "$authority_fd" ]; then
		display_dir="$(job_path "$name")" || return $?
	else
		display_dir="$dir"
	fi
	if [ -z "$authority_fd" ]; then
		exec {observed_job_fd}<"$dir" || die "cannot pin job '$name' generation"
		waited_generation="$("$PYTHON_BIN" - "$observed_job_fd" "$dir" <<'PY'
import os
import stat
import sys

descriptor = int(sys.argv[1])
path = sys.argv[2]
try:
    held = os.fstat(descriptor)
    current = os.stat(path, follow_symlinks=False)
except OSError:
    raise SystemExit(1)
if not stat.S_ISDIR(held.st_mode) or (held.st_dev, held.st_ino) != (
    current.st_dev,
    current.st_ino,
):
    raise SystemExit(1)
print(f"{held.st_dev}:{held.st_ino}")
PY
)" || die "cannot identify job '$name' generation"
	fi

	# Local sleep loop: cheap (no model round-trip), unlike write_stdin polling.
	# The launched PID is the completion authority for an internal run. Do not
	# trust an early exit_code pathname that the same-account command can create;
	# the noninteractive parent reaps the detached executor after it terminates.
	local waited=0
	while { if [ -n "$expected_pid" ]; then kill -0 "$expected_pid" 2>/dev/null; else job_running "$dir"; fi; }; do
		if [ "$waited" -ge "$timeout" ]; then
			# Re-waiting at the same too-short timeout rebuilds the poll loop
			# this tool exists to remove, so suggest a concrete larger value
			# based on how long the job has actually been alive.
			local started elapsed suggest
			started="$(date -u -d "$(cat "$dir/started_at" 2>/dev/null)" +%s 2>/dev/null || echo 0)"
			if [ "$started" -gt 0 ]; then
				elapsed=$(($(date -u +%s) - started))
			else
				elapsed="$waited"
			fi
			suggest=$((elapsed * 2))
			[ "$suggest" -lt 60 ] && suggest=60
			echo "job '$name' still running after ${timeout}s (alive ${elapsed}s, pid ${expected_pid:-$(cat "$dir/pid" 2>/dev/null)})" >&2
			echo "--- last $tail_lines log lines ---" >&2
			tail -n "$tail_lines" "$dir/log" >&2 2>/dev/null || true
			echo "full log: $display_dir/log" >&2
			echo "Do NOT re-wait at --timeout $timeout; that is a poll loop. Use:" >&2
			echo "  scripts/util/job.sh wait $name --timeout $suggest" >&2
			return 124
		fi
		sleep 2
		waited=$((waited + 2))
	done

	local wait_lease_fd="" current_dir current_generation code=1
	if [ -z "$authority_fd" ]; then
		if ! acquire_job_lease "$name" wait_lease_fd; then
			die "job '$name' lifecycle changed before its result could be reaped; retry wait"
		fi
		current_dir="$(job_path "$name")" || {
			close_job_lease "$wait_lease_fd"
			return 2
		}
		current_generation="$(stat -Lc '%d:%i' -- "$current_dir" 2>/dev/null || true)"
		if [ -z "$current_generation" ] || [ "$current_generation" != "$waited_generation" ] ||
			[ "$(stat -Lc '%d:%i' -- "/proc/$$/fd/$observed_job_fd" 2>/dev/null || true)" != "$waited_generation" ]; then
			close_job_lease "$wait_lease_fd"
			die "job '$name' generation changed before its result could be reaped; retry wait"
		fi
		display_dir="$current_dir"
		dir="/proc/$$/fd/$observed_job_fd"
	fi
	if [ -f "$dir/exit_code" ] && [ ! -L "$dir/exit_code" ]; then
		code="$(cat "$dir/exit_code" 2>/dev/null || echo 1)"
		# Only a sealed terminal may be reaped by waiting.  Supervisor loss stays
		# visible and unreaped until reconcile seals its typed recovery receipt.
		write_waited_marker "$dir" || die "job '$name' waited marker is unsafe"
	else
		if [ -f "$dir/binding.json" ] && [ ! -L "$dir/binding.json" ]; then
			echo "job '$name' has no sealed exit code; run 'reconcile $name' to classify supervisor loss" >&2
		else
			echo "job '$name' has no sealed exit code or delivery binding; inspect its log, then clean --all if the result is not needed" >&2
		fi
	fi
	emit_result "$dir" "$name" "$tail_lines" "$code" "$display_dir"
	close_job_lease "$wait_lease_fd"
	[ -z "$observed_job_fd" ] || eval "exec ${observed_job_fd}>&-"
	return "$code"
}

# Block until a dispatched worker's terminal artifact exists (#3418 P1).
# The model makes ONE call; this shell-side loop does the watching — the
# event-driven wait shape, with the timeout demoted to a deadman.
cmd_wait_file() {
	local path="${1:-}"
	shift || true
	[ -n "$path" ] || die "wait-file requires a path"
	local timeout="$DEFAULT_TIMEOUT" tail_lines="$DEFAULT_TAIL" fast_cadence=""
	while [ "$#" -gt 0 ]; do
		case "$1" in
		--timeout)
			timeout="${2:-}"
			shift 2
			;;
		--tail)
			tail_lines="${2:-}"
			shift 2
			;;
		--fast-cadence)
			fast_cadence="${2:-}"
			shift 2
			;;
		*) die "unknown option '$1'" ;;
		esac
	done
	case "$timeout" in '' | *[!0-9]*) die "--timeout must be a whole number of seconds" ;; esac
	case "$tail_lines" in '' | *[!0-9]*) die "--tail must be a whole number of lines" ;; esac
	warn_short_timeout "$timeout" "$fast_cadence" "wait-file"

	local waited=0
	while [ ! -e "$path" ]; do
		if [ "$waited" -ge "$timeout" ]; then
			echo "wait-file: '$path' did not appear within ${timeout}s" >&2
			echo "Deadman fired: the worker is presumed dead. Run the orphan/lease" >&2
			echo "check for its dispatch (agent_dispatch.py doctor / finding-lease)," >&2
			echo "do NOT re-wait blindly — a longer wait cannot revive a dead worker." >&2
			return 124
		fi
		sleep 2
		waited=$((waited + 2))
	done
	echo "wait-file: '$path' present ($(wc -c <"$path" 2>/dev/null || echo '?') bytes)"
	echo "--- first $tail_lines lines ---"
	head -n "$tail_lines" "$path" 2>/dev/null || true
	echo "--- end (full artifact: $path) ---"
	return 0
}

cmd_run() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	local timeout="$DEFAULT_TIMEOUT" tail_lines="$DEFAULT_TAIL"
	local secure_repro_run=0
	local bound_task_id="" bound_terminal_artifact=""
	local -a binding_args=()
	while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
		case "$1" in
		--timeout)
			timeout="${2:-}"
			shift 2
			;;
		--tail)
			tail_lines="${2:-}"
			shift 2
			;;
		--run-id | --task-id | --terminal-artifact)
			[ "$#" -ge 2 ] || die "$1 requires a value"
			[ "$1" != "--task-id" ] || bound_task_id="${2:-}"
			[ "$1" != "--terminal-artifact" ] || bound_terminal_artifact="${2:-}"
			binding_args+=("$1" "${2:-}")
			shift 2
			;;
		*) die "unknown option '$1'" ;;
		esac
	done
	[ "${1:-}" = "--" ] || die "expected '--' before the command"
	shift
	if [ "$#" -eq 10 ] &&
		[[ "$bound_task_id" == final-ci-repro:* ]] &&
		[ "$(realpath -e -- "$(command -v -- "${1:-}" 2>/dev/null)" 2>/dev/null)" = "$(realpath -e -- "$PYTHON_BIN" 2>/dev/null)" ] &&
		[ ! -L "${2:-}" ] &&
		[ "$(realpath -e -- "${2:-}" 2>/dev/null)" = "$REPO_ROOT/scripts/util/final_ci_gate.py" ] &&
		[ "${3:-}" = "--pr" ] && [[ "${4:-}" =~ ^[1-9][0-9]*$ ]] &&
		[ "${5:-}" = "--repo" ] && [ "$(realpath -m -- "${6:-}")" = "$REPO_ROOT" ] &&
		[ "${7:-}" = "--execute-repro" ] && [[ "${8:-}" =~ ^[0-9a-f]{64}$ ]] &&
		[ "$bound_task_id" = "final-ci-repro:${8:-}" ] &&
		[ "${9:-}" = "--terminal-artifact" ] &&
		[ "$(realpath -m -- "${10:-}")" = "$(realpath -m -- "$bound_terminal_artifact")" ]; then
		secure_repro_run=1
	fi
	HOLD_AUTHORITY_FOR_RUN=1
	cmd_start "$name" "${binding_args[@]}" -- "$@" >/dev/null || return $?
	HOLD_AUTHORITY_FOR_RUN=0
	RUN_INTERNAL_WAIT=1
	cmd_wait "$name" --timeout "$timeout" --tail "$tail_lines" \
		--authority-fd "$RUN_AUTHORITY_FD" --expected-pid "$RUN_EXECUTOR_PID"
	local result=$?
	RUN_INTERNAL_WAIT=0
	if [ "$result" -eq 124 ] && [ "$secure_repro_run" -eq 1 ]; then
		# A bound reproduction cannot be resumed after its caller releases the
		# held directory authority. Killing the executor closes the authenticated
		# lifetime channel, which makes the transient service kill its whole group.
		kill -TERM "$RUN_EXECUTOR_PID" 2>/dev/null || true
		wait "$RUN_EXECUTOR_PID" 2>/dev/null || true
		while kill -0 "$RUN_EXECUTOR_PID" 2>/dev/null; do sleep 0.1; done
	fi
	exec {RUN_AUTHORITY_FD}<&-
	RUN_EXECUTOR_PID=""
	return "$result"
}

cmd_status() {
	local name="${1:-}"
	validate_name "$name"
	local dir
	dir="$(job_path "$name")" || return $?
	[ -d "$dir" ] || die "no such job '$name'"
	if job_running "$dir"; then
		local hint=""
		hint="$(missing_lease_terminal_hint "$dir" "$name" || true)"
		echo "job '$name': RUNNING (pid $(cat "$dir/pid" 2>/dev/null), started $(cat "$dir/started_at" 2>/dev/null))${hint:+; $hint}"
		return 0
	fi
	local code
	code="$(cat "$dir/exit_code" 2>/dev/null || echo '?')"
	echo "job '$name': DONE exit=$code (started $(cat "$dir/started_at" 2>/dev/null))"
	[ "$code" = "0" ]
}

cmd_log() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	local tail_lines="$DEFAULT_TAIL" follow=0
	while [ "$#" -gt 0 ]; do
		case "$1" in
		--tail)
			tail_lines="${2:-}"
			shift 2
			;;
		--follow)
			follow=1
			shift
			;;
		*) die "unknown option '$1'" ;;
		esac
	done
	local dir
	dir="$(job_path "$name")" || return $?
	[ -d "$dir" ] || die "no such job '$name'"
	if [ "$follow" = "1" ]; then
		tail -n "$tail_lines" -f "$dir/log"
	else
		tail -n "$tail_lines" "$dir/log"
	fi
}

cmd_list() {
	local found=0 root dir
	for root in "${JOB_ROOTS[@]}"; do
		[ -d "$root" ] || continue
		for dir in "$root"/*/; do
			[ -d "$dir" ] || continue
			found=1
			local name state
			name="$(basename "$dir")"
			if job_running "$dir"; then
				state="RUNNING"
			else
				state="exit=$(cat "$dir/exit_code" 2>/dev/null || echo '?')"
			fi
			printf '%-28s %-10s %s  %s\n' "$name" "$state" \
				"$(cat "$dir/started_at" 2>/dev/null || echo '-')" \
				"$(head -c 80 "$dir/cmd" 2>/dev/null | tr '\n' ' ')"
		done
	done
	[ "$found" = "1" ] || echo "no jobs"
}

# A job whose result nobody reads is worse than a poll loop: the work happened,
# the failure is invisible, and the turn ends believing it succeeded.
cmd_check() {
	local running=0 unreaped=0 root dir
	for root in "${JOB_ROOTS[@]}"; do
		[ -d "$root" ] || continue
		for dir in "$root"/*/; do
			[ -d "$dir" ] || continue
			local name
			name="$(basename "$dir")"
			# A detached command may run a final hygiene check itself. Ignore only
			# its own wrapper, matched to the create-once executor identity; every
			# sibling job and every stale result remains visible. Terminal authority
			# stays in the recorded supervisor and is never exposed to the command.
			if [ "$name" = "${INTELFLO_JOB_NAME:-}" ] &&
				[ -n "${INTELFLO_JOB_EXECUTOR_PID:-}" ] &&
				executor_matches "$dir" "$name" "$INTELFLO_JOB_EXECUTOR_PID"; then
				continue
			fi
			if job_running "$dir"; then
				running=$((running + 1))
				echo "job '$name' is still RUNNING (pid $(cat "$dir/pid" 2>/dev/null))" >&2
				local hint=""
				hint="$(missing_lease_terminal_hint "$dir" "$name" || true)"
				[ -z "$hint" ] || echo "  $hint" >&2
			elif ! job_reaped "$dir"; then
				unreaped=$((unreaped + 1))
				echo "job '$name' finished with exit $(cat "$dir/exit_code" 2>/dev/null || echo '?') but was never waited on" >&2
			fi
		done
	done
	if [ "$running" -gt 0 ] || [ "$unreaped" -gt 0 ]; then
		echo "" >&2
		echo "$running running, $unreaped unreaped. Wait on each before finishing:" >&2
		echo "  scripts/util/job.sh wait <name> --timeout <seconds>" >&2
		echo "  scripts/util/job.sh clean --all   # only if the results are genuinely not needed" >&2
		return 1
	fi
	return 0
}

cmd_binding_files() {
	[ "${1:-}" = "--run-id" ] || die "binding-files requires --run-id <id>"
	local run_id="${2:-}"
	[[ "$run_id" =~ ^sr_[0-9a-f]{32}$ ]] || die "binding-files --run-id must match sr_<32 lowercase hex>"
	local skip_name=""
	if [ "${INTELFLO_TRUSTED_CONTINUATION:-}" = "1" ] &&
		[ -n "${INTELFLO_JOB_NAME:-}" ] &&
		[ -n "${INTELFLO_JOB_EXECUTOR_PID:-}" ]; then
		local current_dir
		current_dir="$(job_path "$INTELFLO_JOB_NAME")" || return $?
		if executor_matches "$current_dir" "$INTELFLO_JOB_NAME" "$INTELFLO_JOB_EXECUTOR_PID"; then
			skip_name="$INTELFLO_JOB_NAME"
		fi
	fi
	"$PYTHON_BIN" - "$run_id" "$skip_name" "${JOB_ROOTS[@]}" <<'PY'
import json
import sys
from pathlib import Path

run_id = sys.argv[1]
skip_name = sys.argv[2]
roots = [Path(raw) for raw in sys.argv[3:]]
paths = sorted(
    path
    for root in roots
    if root.is_dir()
    for path in root.glob("*/binding.json")
)
for path in paths:
    if skip_name and path.parent.name == skip_name:
        continue
    if path.is_symlink():
        raise SystemExit(f"job.sh: invalid symlinked job binding {path}")
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"job.sh: invalid job binding {path}: {exc}")
    required_by_schema = {
        "job-binding-v1": {
            "schema_version", "name", "run_id", "task_id", "terminal_artifact"
        },
        "job-binding-v2": {
            "schema_version", "name", "run_id", "task_id", "terminal_envelope"
        },
    }
    required = required_by_schema.get(binding.get("schema_version"))
    if required is None or set(binding) != required:
        raise SystemExit(f"job.sh: invalid job binding schema at {path}")
    if binding.get("name") != path.parent.name:
        raise SystemExit(f"job.sh: job binding name does not match {path.parent}")
    if binding.get("run_id") == run_id:
        print(path.resolve())
PY
}

cmd_reconcile() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	local dir lease_fd=""
	dir="$(job_path "$name")" || return $?
	# Avoid allocating a permanent per-name lease for a typo or unknown job.
	# A concurrent start after this check owns the new job and this reconcile
	# still fails without touching it.
	[ -d "$dir" ] || die "no such job '$name'"
	if ! acquire_job_lease "$name" lease_fd; then
		die "job '$name' still has an active launcher or supervisor"
	fi
	# The directory and every durable terminal file are read only after the
	# lifecycle lease is acquired; PID state alone is never terminal authority.
	[ -d "$dir" ] || die "no such job '$name'"
	[ ! -L "$dir" ] || die "job '$name' directory cannot be a symlink"
	[ -f "$dir/binding.json" ] || die "job '$name' has no delivery-run binding"
	[ ! -L "$dir/binding.json" ] || die "job '$name' binding cannot be a symlink"
	local binding_schema supplied_artifact="" primary_repo=""
	binding_schema="$("$PYTHON_BIN" - "$dir/binding.json" <<'PY'
import json
import os
import stat
import sys

try:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(sys.argv[1], flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise SystemExit("job.sh: job binding is not a safe file")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as stream:
            binding = json.load(stream)
    finally:
        os.close(descriptor)
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"job.sh: invalid job binding: {exc}") from exc
schema = binding.get("schema_version")
if not isinstance(schema, str):
    raise SystemExit("job.sh: invalid job binding schema")
print(schema)
PY
)" || return $?
	case "$binding_schema" in
	job-binding-v2)
		[ "${1:-}" != "--terminal-artifact" ] || die "v2 reconciliation uses its durable terminal envelope; external artifact overrides are forbidden"
		;;
	job-binding-v1)
		[ "${1:-}" = "--terminal-artifact" ] && [ -n "${2:-}" ] ||
			die "legacy v1 reconcile requires --terminal-artifact <path>"
		supplied_artifact="$(realpath -m -- "$2")"
		shift 2
		;;
	*) die "job '$name' binding schema is invalid" ;;
	esac
	if [ "${1:-}" = "--primary" ]; then
		primary_repo="${2:-}"
		[ -n "$primary_repo" ] || die "reconcile --primary requires a path"
		shift 2
	fi
	[ "$#" -eq 0 ] || die "reconcile received unexpected arguments"
	local git_common_dir dispatch_root=""
	if [ -n "$primary_repo" ]; then
		primary_repo="$(realpath -e -- "$primary_repo")" || die "reconcile primary repository is unreadable"
		git_common_dir="$(git -C "$primary_repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ||
			die "cannot resolve the primary repository for job reconciliation"
		[ "$(dirname "$git_common_dir")" = "$primary_repo" ] ||
			die "reconcile --primary must name the canonical primary repository"
		dispatch_root="$primary_repo/.audit/dispatch"
	fi
	"$PYTHON_BIN" - "$dir/binding.json" "$supplied_artifact" "$dir/exit_code" "$dir/reconciliation.json" "$dispatch_root" <<'PY'
import hashlib
import errno
import json
import os
import re
import stat
import sys
from pathlib import Path

binding_path = Path(sys.argv[1])
supplied_artifact = sys.argv[2]
exit_path = Path(sys.argv[3])
receipt_path = Path(sys.argv[4])
declared_dispatch_root = sys.argv[5]

def read_safe_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SystemExit(f"job.sh: {label} cannot be a symlink") from exc
        raise SystemExit(f"job.sh: {label} is unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise SystemExit(f"job.sh: {label} is not a safe file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read()
    finally:
        os.close(descriptor)

def commit_receipt(serialized: str, conflict_message: str) -> None:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    directory_fd = os.open(receipt_path.parent, directory_flags)
    receipt_name = receipt_path.name
    temporary_name = receipt_name + ".tmp"

    def read_existing() -> bytes | None:
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(receipt_name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SystemExit(
                    "job.sh: reconciliation path cannot be a symlink"
                ) from exc
            raise SystemExit("job.sh: reconciliation path is unreadable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise SystemExit("job.sh: reconciliation path is not a safe file")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                return stream.read()
        finally:
            os.close(descriptor)

    encoded = serialized.encode("utf-8")
    try:
        existing = read_existing()
        if existing is not None:
            if existing != encoded:
                raise SystemExit(conflict_message)
            return

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            temporary_fd = os.open(
                temporary_name, flags, 0o600, dir_fd=directory_fd
            )
        except FileExistsError as exc:
            metadata = os.stat(
                temporary_name, dir_fd=directory_fd, follow_symlinks=False
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise SystemExit(
                    "job.sh: reconciliation temporary path cannot be a symlink"
                ) from exc
            raise SystemExit(
                "job.sh: reconciliation temporary path already exists"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SystemExit(
                    "job.sh: reconciliation temporary path cannot be a symlink"
                ) from exc
            raise
        try:
            with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(temporary_fd)

        try:
            os.link(
                temporary_name,
                receipt_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except FileExistsError:
            existing = read_existing()
            if existing != encoded:
                raise SystemExit(conflict_message)
        finally:
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

try:
    binding_bytes = read_safe_bytes(binding_path, "job binding")
    binding = json.loads(binding_bytes)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"job.sh: job binding is unreadable: {exc}")
schema = binding.get("schema_version")
required_by_schema = {
    "job-binding-v1": {
        "schema_version", "name", "run_id", "task_id", "terminal_artifact"
    },
    "job-binding-v2": {
        "schema_version", "name", "run_id", "task_id", "terminal_envelope"
    },
}
required = required_by_schema.get(schema)
if required is None or set(binding) != required:
    raise SystemExit("job.sh: job binding schema is invalid")
if binding.get("name") != binding_path.parent.name:
    raise SystemExit("job.sh: job binding name does not match its job directory")
if not isinstance(binding.get("run_id"), str) or re.fullmatch(r"sr_[0-9a-f]{32}", binding["run_id"]) is None:
    raise SystemExit("job.sh: job binding run_id is invalid")
if not isinstance(binding.get("task_id"), str) or not binding["task_id"].strip():
    raise SystemExit("job.sh: job binding task_id is invalid")

exit_code = None
if exit_path.is_symlink():
    raise SystemExit("job.sh: terminal exit code cannot be a symlink")
if not exit_path.exists():
    if schema != "job-binding-v2":
        raise SystemExit("job.sh: legacy job has no terminal exit code")
    envelope_path = binding_path.parent / "terminal-envelope.json"
    digest_path = binding_path.parent / "terminal-envelope.sha256"
    if envelope_path.is_symlink() or digest_path.is_symlink():
        raise SystemExit(
            "job.sh: durable terminal files cannot be symlinks"
        )
    envelope_exists = envelope_path.exists()
    digest_exists = digest_path.exists()
    if envelope_exists != digest_exists:
        raise SystemExit(
            "job.sh: unsealed job has conflicting partial durable terminal files"
        )
    if not envelope_exists:
        receipt = {
            "schema_version": "job-supervisor-loss-v1",
            "name": binding["name"],
            "run_id": binding["run_id"],
            "task_id": binding["task_id"],
            "failure_class": "supervisor-loss",
            "reason": (
                "startup-failure"
                if not (binding_path.parent / "pid").exists()
                else "supervisor-loss"
            ),
            "binding_sha256": hashlib.sha256(binding_bytes).hexdigest(),
        }
        serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
        commit_receipt(
            serialized,
            "job.sh: existing job reconciliation conflicts with supervisor loss",
        )
        print(json.dumps(receipt, sort_keys=True))
        raise SystemExit(0)
else:
    try:
        exit_code = int(read_safe_bytes(exit_path, "terminal exit code").decode("utf-8").strip())
    except (UnicodeError, ValueError) as exc:
        raise SystemExit(f"job.sh: authoritative terminal artifact is unreadable: {exc}")

envelope_path = None
envelope_sha256 = None
if schema == "job-binding-v1":
    artifact_path = Path(supplied_artifact)
    if not isinstance(binding.get("terminal_artifact"), str):
        raise SystemExit("job.sh: job binding terminal artifact is invalid")
    if Path(binding["terminal_artifact"]).resolve() != artifact_path.resolve():
        raise SystemExit("job.sh: terminal artifact does not match the job binding")
    if artifact_path.is_symlink():
        raise SystemExit("job.sh: terminal artifact cannot be a symlink")
    try:
        artifact_bytes = read_safe_bytes(
            artifact_path, "authoritative terminal artifact"
        )
        artifact = json.loads(artifact_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"job.sh: authoritative terminal artifact is unreadable: {exc}") from exc
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
else:
    terminal_envelope = binding.get("terminal_envelope")
    if not isinstance(terminal_envelope, str) or not Path(terminal_envelope).is_absolute():
        raise SystemExit("job.sh: job binding terminal envelope is invalid")
    envelope_path = Path(terminal_envelope)
    expected_envelope = binding_path.parent / "terminal-envelope.json"
    if envelope_path.resolve() != expected_envelope.resolve():
        raise SystemExit("job.sh: terminal envelope does not match the job binding")
    digest_path = binding_path.parent / "terminal-envelope.sha256"
    if envelope_path.is_symlink() or digest_path.is_symlink():
        raise SystemExit("job.sh: durable terminal envelope cannot be a symlink")
    try:
        envelope_bytes = read_safe_bytes(envelope_path, "durable terminal envelope")
        recorded_digest = read_safe_bytes(
            digest_path, "durable terminal envelope digest"
        ).decode("ascii").strip()
        envelope = json.loads(envelope_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"job.sh: durable terminal envelope is unreadable: {exc}") from exc
    envelope_sha256 = hashlib.sha256(envelope_bytes).hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", recorded_digest) is None or recorded_digest != envelope_sha256:
        raise SystemExit("job.sh: durable terminal envelope digest does not match")
    envelope_required = {
        "schema_version", "name", "run_id", "task_id",
        "source_terminal_artifact", "terminal_artifact_sha256",
        "command_exit_code", "terminal",
    }
    if set(envelope) != envelope_required or envelope.get("schema_version") != "job-terminal-envelope-v2":
        raise SystemExit("job.sh: durable terminal envelope schema is invalid")
    for key in ("name", "run_id", "task_id"):
        if envelope.get(key) != binding.get(key):
            raise SystemExit(f"job.sh: durable terminal envelope {key} does not match the job binding")
    if (
        not isinstance(envelope.get("source_terminal_artifact"), str)
        or not Path(envelope["source_terminal_artifact"]).is_absolute()
        or not isinstance(envelope.get("terminal_artifact_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", envelope["terminal_artifact_sha256"]) is None
        or not isinstance(envelope.get("command_exit_code"), int)
        or isinstance(envelope.get("command_exit_code"), bool)
    ):
        raise SystemExit("job.sh: durable terminal envelope authority is invalid")
    if exit_code is None:
        exit_code = envelope["command_exit_code"]
    elif envelope["command_exit_code"] != exit_code:
        raise SystemExit("job.sh: durable terminal envelope authority is invalid")
    artifact_path = Path(envelope["source_terminal_artifact"])
    artifact_sha256 = envelope["terminal_artifact_sha256"]
    artifact = envelope.get("terminal")
if not isinstance(artifact, dict) or artifact.get("task_id") != binding.get("task_id"):
    raise SystemExit("job.sh: terminal artifact task_id does not match the job binding")
status = artifact.get("status")
terminal_statuses = {
    "completed",
    "needs-escalation",
    "scope-violation",
    "acceptance-failure",
    "infrastructure-failure",
    "packaging-failure",
    "failed",
}
terminal_record_sha256 = None
if status == "blocked":
    dispatch_root = (
        Path(declared_dispatch_root)
        if declared_dispatch_root
        else artifact_path.parent.parent
    )
    audit_root = dispatch_root.parent
    if (
        artifact_path.parent.resolve() != (dispatch_root / "results").resolve()
        or dispatch_root.name != "dispatch"
        or audit_root.name != ".audit"
        or any(
            component.is_symlink()
            for component in (artifact_path.parent, dispatch_root, audit_root)
        )
    ):
        raise SystemExit(
            "job.sh: blocked result has no matching dispatcher terminal telemetry"
        )
    repo_root = audit_root.parent
    sys.path.insert(0, str(repo_root / "scripts" / "util"))
    from agent_dispatch import DispatchError, load_authority_records

    # Keep this explicit allowlist aligned with agent_dispatch.py's compatible
    # telemetry contract; every other row is forensic evidence, not authority.
    compatible_terminal_telemetry = {
        ("dispatch-telemetry-v9", "2026-07-24-v9"),
        ("dispatch-telemetry-v9", "2026-08-06-v10"),
        ("dispatch-telemetry-v9", "2026-08-17-v11"),
    }
    matching_records = []
    try:
        authority_records = load_authority_records(repo_root, 30)
    except DispatchError as exc:
        raise SystemExit(
            f"job.sh: dispatcher terminal telemetry is invalid: {exc}"
        ) from exc
    for record in authority_records:
            result_artifact = record.get("result_artifact")
            if not isinstance(result_artifact, str):
                continue
            recorded_path = Path(result_artifact)
            if not recorded_path.is_absolute():
                recorded_path = repo_root / recorded_path
            recorded_exit_code = record.get("exit_code")
            if (
                record.get("type") == "attempt-terminal"
                and (
                    record.get("schema_version"),
                    record.get("policy_version"),
                )
                in compatible_terminal_telemetry
                and record.get("runtime_contract_version") in {None, 4}
                and record.get("task_id") == binding["task_id"]
                and record.get("run_id") == binding["run_id"]
                and record.get("status") == "blocked"
                and isinstance(recorded_exit_code, int)
                and not isinstance(recorded_exit_code, bool)
                and recorded_exit_code == exit_code == 0
                and recorded_path.resolve() == artifact_path.resolve()
                and record.get("result_sha256") == artifact_sha256
            ):
                matching_records.append(record)
    matching_records_by_digest = {
        hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(): record
        for record in matching_records
    }
    if len(matching_records_by_digest) != 1:
        raise SystemExit(
            "job.sh: blocked result has no unique matching dispatcher terminal telemetry"
        )
    terminal_record_sha256 = next(iter(matching_records_by_digest))
elif status == "repair-required":
    if exit_code != 3:
        raise SystemExit(
            "job.sh: repair-required terminal status conflicts with the wrapper exit code"
        )
elif status not in terminal_statuses:
    raise SystemExit("job.sh: terminal artifact status is not authoritative")
elif (status == "completed") != (exit_code == 0):
    raise SystemExit("job.sh: terminal artifact status conflicts with the wrapper exit code")
receipt = {
    "schema_version": "job-reconciliation-v1" if schema == "job-binding-v1" else "job-reconciliation-v2",
    "name": binding["name"],
    "run_id": binding["run_id"],
    "task_id": binding["task_id"],
    "exit_code": exit_code,
    "terminal_status": status,
    "terminal_artifact_sha256": artifact_sha256,
}
if schema == "job-binding-v1":
    receipt["terminal_artifact"] = str(artifact_path.resolve())
else:
    receipt["terminal_envelope"] = str(envelope_path.resolve())
    receipt["terminal_envelope_sha256"] = envelope_sha256
    receipt["source_terminal_artifact"] = str(artifact_path.resolve())
if terminal_record_sha256 is not None:
    receipt["terminal_authority"] = "dispatcher-attempt-terminal"
    receipt["terminal_record_sha256"] = terminal_record_sha256
serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
commit_receipt(
    serialized,
    "job.sh: existing job reconciliation conflicts with this artifact",
)
print(json.dumps(receipt, sort_keys=True))
PY
	local status=$?
	[ "$status" -eq 0 ] || return "$status"
	write_waited_marker "$dir" || die "job '$name' waited marker is unsafe"
}

cmd_clean() {
	local all=0
	[ "${1:-}" = "--all" ] && all=1
	local root dir failures=0
	for root in "${JOB_ROOTS[@]}"; do
		[ -d "$root" ] || continue
		for dir in "$root"/*/; do
			[ -d "$dir" ] || continue
			local name lease_fd="" lease_path attempt=0
			name="$(basename "$dir")"
			lease_path="$(dirname "$dir")/.leases/$name.lock"
			if [ ! -e "$lease_path" ] && [ ! -L "$lease_path" ] &&
				! job_reaped "$dir" &&
				{ { [ -f "$dir/pid" ] && [ ! -L "$dir/pid" ]; } ||
					{ [ -f "$dir/binding.json" ] && [ ! -L "$dir/binding.json" ]; }; }; then
				if [ "$all" = "1" ]; then
					echo "job '$name' has live evidence but its lifecycle lease is missing; refusing forced cleanup" >&2
					failures=$((failures + 1))
				fi
				continue
			fi
			if ! acquire_job_lease "$name" lease_fd; then
				[ "$all" = "1" ] || continue
				if ! lease_path="$(job_lease_path "$name")"; then
					echo "job '$name' cannot resolve its stable lifecycle lease for forced cleanup" >&2
					failures=$((failures + 1))
					continue
				fi
				if ! terminate_job_lease_owner "$dir/pid" "$dir/pid-identity.json" "$lease_path"; then
					echo "job '$name' cannot authenticate its lifecycle lease owner for forced cleanup" >&2
					failures=$((failures + 1))
					continue
				fi
				while ! acquire_job_lease "$name" lease_fd; do
					attempt=$((attempt + 1))
					if [ "$attempt" -ge 200 ]; then
						echo "job '$name' supervisor did not release its lifecycle lease" >&2
						failures=$((failures + 1))
						break
					fi
					sleep 0.05
				done
				[ -n "$lease_fd" ] || continue
			fi
			if [ "$all" != "1" ] && ! job_reaped "$dir"; then
				close_job_lease "$lease_fd"
				continue
			fi
			rm -rf "$dir"
			close_job_lease "$lease_fd"
		done
	done
	echo "cleaned ${JOB_ROOTS[*]}"
	[ "$failures" -eq 0 ] || return 2
}

main() {
	local sub="${1:-}"
	shift || true
	case "$sub" in
	_execute) cmd_execute "$@" ;;
	start) cmd_start "$@" ;;
	wait) cmd_wait "$@" ;;
	wait-file) cmd_wait_file "$@" ;;
	run) cmd_run "$@" ;;
	status) cmd_status "$@" ;;
	log) cmd_log "$@" ;;
	list) cmd_list "$@" ;;
	binding-files) cmd_binding_files "$@" ;;
	reconcile) cmd_reconcile "$@" ;;
	check) cmd_check "$@" ;;
	clean) cmd_clean "$@" ;;
	-h | --help | help) usage 0 ;;
	*) usage 2 ;;
	esac
}

main "$@"
