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
JOB_DIR="$(python3 "$REPO_ROOT/scripts/util/job_store.py" --worktree "$JOB_WORKTREE" --ensure-root)" || exit $?
JOB_ROOTS=("$JOB_DIR")
DEFAULT_TIMEOUT="${INTELFLO_JOB_TIMEOUT:-1800}"
DEFAULT_TAIL=40

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

cmd_execute() {
	local dir="${1:-}" source_artifact="${2:-}"
	shift 2 || true
	[ "${1:-}" = "--" ] || die "internal executor expected '--' before the command"
	shift
	local name="${INTELFLO_JOB_NAME:-}"
	validate_name "$name"
	[ "$dir" = "$(job_path "$name")" ] || die "internal executor job path is invalid"
	[ -d "$dir" ] && [ ! -L "$dir" ] || die "internal executor job directory is invalid"
	# Replace this shell with the supervisor so the recorded PID is also the
	# process that holds every sealing descriptor. The wrapped command receives
	# no token, claim file, directory descriptor, or pending-artifact descriptor.
	exec python3 - "$dir" "$source_artifact" "$name" "$REPO_ROOT" "$@" <<'PY'
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[4]).resolve() / "scripts" / "util"))
from trusted_executable import TrustedExecutableError, system_executable

job_dir = Path(sys.argv[1])
source_path = Path(sys.argv[2])
job_name = sys.argv[3]
repo_root = Path(sys.argv[4]).resolve()
command = sys.argv[5:]

directory_flags = os.O_RDONLY
if hasattr(os, "O_DIRECTORY"):
    directory_flags |= os.O_DIRECTORY
if hasattr(os, "O_NOFOLLOW"):
    directory_flags |= os.O_NOFOLLOW
directory_fd = os.open(job_dir, directory_flags)

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

    dispatcher = repo_root / "scripts" / "util" / "agent_dispatch.py"
    executable = shutil.which(command[0]) if command else None
    command_script = Path(command[1]) if len(command) > 1 else None
    trusted_dispatcher = (
        binding is not None
        and len(command) >= 3
        and executable is not None
        and Path(executable).resolve() == Path(sys.executable).resolve()
        and not dispatcher.is_symlink()
        and dispatcher.is_file()
        and command_script is not None
        and command_script.resolve() == dispatcher.resolve()
        and command[2] == "run"
        and not job_dir.resolve().is_relative_to(repo_root)
        and not source_path.resolve().is_relative_to(repo_root)
    )
    bwrap = None
    if binding is not None and not trusted_dispatcher:
        try:
            bwrap = str(system_executable("bwrap"))
        except TrustedExecutableError as exc:
            raise RuntimeError(
                "bound job supervision requires protected bubblewrap"
            ) from exc

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
    child_env["INTELFLO_JOB_NAME"] = job_name
    child_env["INTELFLO_JOB_EXECUTOR_PID"] = str(os.getpid())
    wrapped_command = command
    if binding is not None and not trusted_dispatcher:
        assert bwrap is not None
        # Keep the delegated command's normal host view, but turn every
        # ancestor of the job collection into a mount point before making the
        # collection read-only. A child therefore cannot rename an ancestor,
        # recreate the lexical job path, and feed reconciliation forged files.
        # The parent keeps the only writable authority descriptors, and the
        # private PID namespace plus --die-with-parent removes delayed peers.
        # job_store.py resolves exactly one authority root. Legacy recovery is
        # explicit, so the fallback never leaves a second discovered root writable.
        protected_authority_root = job_dir.resolve().parent
        authority_mounts: list[str] = []
        for ancestor in reversed(protected_authority_root.parents):
            if ancestor == Path("/"):
                continue
            authority_mounts.extend(("--bind", str(ancestor), str(ancestor)))
        wrapped_command = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--bind",
            "/",
            "/",
            *authority_mounts,
            "--ro-bind",
            str(protected_authority_root),
            str(protected_authority_root),
            "--proc",
            "/proc",
            "--dev-bind",
            "/dev",
            "/dev",
            "--chdir",
            os.getcwd(),
            "--",
            *command,
        ]
    completed = subprocess.run(
        wrapped_command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=child_env,
        close_fds=True,
    )
    final_code = completed.returncode

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
        elif status not in terminal_statuses:
            raise RuntimeError("terminal artifact status is not authoritative")
        elif (status == "completed") != (final_code == 0):
            raise RuntimeError("terminal artifact status conflicts with the wrapper exit code")
        envelope = {
            "schema_version": "job-terminal-envelope-v2",
            "name": binding["name"],
            "run_id": binding["run_id"],
            "task_id": binding["task_id"],
            "source_terminal_artifact": str(source_path.resolve()),
            "terminal_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "command_exit_code": final_code,
            "terminal": terminal,
        }
        envelope_bytes = (
            json.dumps(envelope, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest_bytes = (
            hashlib.sha256(envelope_bytes).hexdigest() + "\n"
        ).encode("ascii")
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
    for descriptor in (pid_fd, binding_fd, exit_fd, envelope_fd, digest_fd):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    os.close(directory_fd)
    log.close()

raise SystemExit(final_code)
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
		terminal_artifact="$(realpath -m -- "$terminal_artifact")"
	fi

	local dir
	dir="$(job_path "$name")" || return $?
	if [ -f "$dir/pid" ] && kill -0 "$(cat "$dir/pid" 2>/dev/null)" 2>/dev/null; then
		die "job '$name' is already running (pid $(cat "$dir/pid")); use 'wait' or pick another name"
	fi
	rm -rf "$dir"
	local created_dir
	created_dir="$(python3 "$REPO_ROOT/scripts/util/job_store.py" --worktree "$JOB_WORKTREE" --ensure-job-directory "$name")" ||
		die "cannot create protected job directory"
	[ "$created_dir" = "$dir" ] || die "job authority path changed during creation"

	# Record the command before launching so `status` is meaningful even if the
	# process dies immediately.
	printf '%s\n' "$*" >"$dir/cmd"
	date -u '+%Y-%m-%dT%H:%M:%SZ' >"$dir/started_at"
	: >"$dir/log"
	if [ -n "$run_id" ]; then
		python3 - "$dir/binding.json" "$name" "$run_id" "$task_id" "$dir/terminal-envelope.json" <<'PY'
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

	# setsid detaches from the agent's shell session, so the job survives the
	# exec_command timeout that would otherwise orphan or kill it.
	setsid env INTELFLO_JOB_NAME="$name" \
		"$JOB_SCRIPT" _execute "$dir" "$terminal_artifact" -- "$@" \
		</dev/null >/dev/null 2>&1 &
	local pid=$!
	printf '%s\n' "$pid" >"$dir/pid"
	disown "$pid" 2>/dev/null || true

	echo "started job '$name' (pid $pid)"
	echo "  log:  $dir/log"
	echo "  wait: scripts/util/job.sh wait $name --timeout <seconds>"
}

job_running() {
	local dir="$1"
	local pid
	pid="$(cat "$dir/pid" 2>/dev/null || echo '')"
	if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
		return 0
	fi
	return 1
}

emit_result() {
	local dir="$1" name="$2" tail_lines="$3" code="$4"
	echo "job '$name' finished with exit code $code"
	echo "--- last $tail_lines log lines ---"
	tail -n "$tail_lines" "$dir/log" 2>/dev/null || true
	echo "--- end of log ($(wc -l <"$dir/log" 2>/dev/null || echo 0) lines total, full log: $dir/log) ---"
}

cmd_wait() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
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
	warn_short_timeout "$timeout" "$fast_cadence" "wait"

	local dir
	dir="$(job_path "$name")" || return $?
	[ -d "$dir" ] || die "no such job '$name' (try 'job.sh list')"

	# Local sleep loop: cheap (no model round-trip), unlike write_stdin polling.
	local waited=0
	while job_running "$dir"; do
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
			echo "job '$name' still running after ${timeout}s (alive ${elapsed}s, pid $(cat "$dir/pid" 2>/dev/null))" >&2
			echo "--- last $tail_lines log lines ---" >&2
			tail -n "$tail_lines" "$dir/log" >&2 2>/dev/null || true
			echo "Do NOT re-wait at --timeout $timeout; that is a poll loop. Use:" >&2
			echo "  scripts/util/job.sh wait $name --timeout $suggest" >&2
			return 124
		fi
		sleep 2
		waited=$((waited + 2))
	done

	local code
	code="$(cat "$dir/exit_code" 2>/dev/null || echo 1)"
	# Mark the result as read so `check` can distinguish a reaped job from one
	# whose exit code nobody ever looked at.
	: >"$dir/waited"
	emit_result "$dir" "$name" "$tail_lines" "$code"
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
			binding_args+=("$1" "${2:-}")
			shift 2
			;;
		*) die "unknown option '$1'" ;;
		esac
	done
	[ "${1:-}" = "--" ] || die "expected '--' before the command"
	shift
	cmd_start "$name" "${binding_args[@]}" -- "$@" >/dev/null || return $?
	cmd_wait "$name" --timeout "$timeout" --tail "$tail_lines"
}

cmd_status() {
	local name="${1:-}"
	validate_name "$name"
	local dir
	dir="$(job_path "$name")" || return $?
	[ -d "$dir" ] || die "no such job '$name'"
	if job_running "$dir"; then
		echo "job '$name': RUNNING (pid $(cat "$dir/pid" 2>/dev/null), started $(cat "$dir/started_at" 2>/dev/null))"
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
			elif [ ! -f "$dir/waited" ] && [ ! -f "$dir/reconciliation.json" ]; then
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
	python3 - "$run_id" "${JOB_ROOTS[@]}" <<'PY'
import json
import sys
from pathlib import Path

run_id = sys.argv[1]
roots = [Path(raw) for raw in sys.argv[2:]]
paths = sorted(
    path
    for root in roots
    if root.is_dir()
    for path in root.glob("*/binding.json")
)
for path in paths:
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
	local dir
	dir="$(job_path "$name")" || return $?
	[ -d "$dir" ] || die "no such job '$name'"
	[ ! -L "$dir" ] || die "job '$name' directory cannot be a symlink"
	[ -f "$dir/binding.json" ] || die "job '$name' has no delivery-run binding"
	[ ! -L "$dir/binding.json" ] || die "job '$name' binding cannot be a symlink"
	if job_running "$dir"; then
		die "job '$name' is still running"
	fi
	[ -f "$dir/exit_code" ] || die "job '$name' has no terminal exit code"
	local binding_schema supplied_artifact="" primary_repo=""
	binding_schema="$(python3 - "$dir/binding.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    binding = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
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
	python3 - "$dir/binding.json" "$supplied_artifact" "$dir/exit_code" "$dir/reconciliation.json" "$dispatch_root" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

binding_path = Path(sys.argv[1])
supplied_artifact = sys.argv[2]
exit_path = Path(sys.argv[3])
receipt_path = Path(sys.argv[4])
declared_dispatch_root = sys.argv[5]
try:
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    exit_code = int(exit_path.read_text(encoding="utf-8").strip())
except (OSError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"job.sh: authoritative terminal artifact is unreadable: {exc}")
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
        artifact_bytes = artifact_path.read_bytes()
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
        envelope_bytes = envelope_path.read_bytes()
        recorded_digest = digest_path.read_text(encoding="ascii").strip()
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
        or envelope["command_exit_code"] != exit_code
    ):
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
    # Keep this explicit allowlist aligned with agent_dispatch.py's compatible
    # telemetry contract; every other row is forensic evidence, not authority.
    compatible_terminal_telemetry = {
        ("dispatch-telemetry-v9", "2026-07-24-v9"),
        ("dispatch-telemetry-v9", "2026-08-06-v10"),
        ("dispatch-telemetry-v9", "2026-08-17-v11"),
    }
    matching_records = []
    for telemetry_path in sorted(dispatch_root.glob("*.jsonl")):
        if telemetry_path.is_symlink() or not telemetry_path.is_file():
            continue
        try:
            lines = telemetry_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SystemExit(
                f"job.sh: dispatcher terminal telemetry is unreadable: {exc}"
            ) from exc
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"job.sh: dispatcher terminal telemetry is invalid: {exc}"
                ) from exc
            if not isinstance(record, dict):
                continue
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
if receipt_path.exists() and receipt_path.read_text(encoding="utf-8") != serialized:
    raise SystemExit("job.sh: existing job reconciliation conflicts with this artifact")
temporary = receipt_path.with_name(receipt_path.name + ".tmp")
temporary.write_text(serialized, encoding="utf-8")
temporary.replace(receipt_path)
print(json.dumps(receipt, sort_keys=True))
PY
	local status=$?
	[ "$status" -eq 0 ] || return "$status"
	: >"$dir/waited"
}

cmd_clean() {
	local all=0
	[ "${1:-}" = "--all" ] && all=1
	local root dir
	for root in "${JOB_ROOTS[@]}"; do
		[ -d "$root" ] || continue
		for dir in "$root"/*/; do
			[ -d "$dir" ] || continue
			if job_running "$dir"; then
				[ "$all" = "1" ] || continue
				kill -TERM "$(cat "$dir/pid" 2>/dev/null)" 2>/dev/null || true
			fi
			rm -rf "$dir"
		done
	done
	echo "cleaned ${JOB_ROOTS[*]}"
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
