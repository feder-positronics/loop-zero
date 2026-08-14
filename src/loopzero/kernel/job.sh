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
#   scripts/util/job.sh reconcile <name> --terminal-artifact <path>
#   scripts/util/job.sh check                 # non-zero if any job is running or unreaped
#   scripts/util/job.sh clean [--all]
#
# `wait` exits with the job's own exit code, 124 on timeout, 2 on usage error.
# `wait-file` blocks until <path> exists (a dispatched worker's terminal
# artifact is the wait event — #3418 P1); exit 0 on appearance, 124 on the
# deadman timeout, which means "worker presumed dead: run the orphan/lease
# check", never "poll again". Timeouts under 300s draw a warning unless
# --fast-cadence states why the watched state's own cadence is faster.
# Jobs live under $INTELFLO_JOB_DIR (default .pid/jobs/) inside the worktree, so
# parallel worktrees never collide.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOB_DIR="${INTELFLO_JOB_DIR:-$REPO_ROOT/.pid/jobs}"
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

job_path() { printf '%s/%s' "$JOB_DIR" "$1"; }

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
	dir="$(job_path "$name")"
	if [ -f "$dir/pid" ] && kill -0 "$(cat "$dir/pid" 2>/dev/null)" 2>/dev/null; then
		die "job '$name' is already running (pid $(cat "$dir/pid")); use 'wait' or pick another name"
	fi
	rm -rf "$dir"
	mkdir -p "$dir" || die "cannot create $dir"

	# Record the command before launching so `status` is meaningful even if the
	# process dies immediately.
	printf '%s\n' "$*" >"$dir/cmd"
	date -u '+%Y-%m-%dT%H:%M:%SZ' >"$dir/started_at"
	: >"$dir/log"
	local token
	token="$(tr -d '\n' </proc/sys/kernel/random/uuid)"
	printf '%s\n' "$token" >"$dir/token"
	if [ -n "$run_id" ]; then
		python3 - "$dir/binding.json" "$name" "$run_id" "$task_id" "$terminal_artifact" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": "job-binding-v1",
    "name": sys.argv[2],
    "run_id": sys.argv[3],
    "task_id": sys.argv[4],
    "terminal_artifact": sys.argv[5],
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
	setsid env INTELFLO_JOB_NAME="$name" INTELFLO_JOB_TOKEN="$token" bash -c '
		"$@" >>"$0/log" 2>&1
		echo $? >"$0/exit_code"
	' "$dir" "$@" </dev/null >/dev/null 2>&1 &
	local pid=$!
	printf '%s\n' "$pid" >"$dir/pid"
	disown "$pid" 2>/dev/null || true

	echo "started job '$name' (pid $pid)"
	echo "  log:  $dir/log"
	echo "  wait: scripts/util/job.sh wait $name --timeout <seconds>"
}

job_running() {
	local dir="$1"
	[ -f "$dir/exit_code" ] && return 1
	local pid
	pid="$(cat "$dir/pid" 2>/dev/null || echo '')"
	[ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
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
	dir="$(job_path "$name")"
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
	dir="$(job_path "$name")"
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
	dir="$(job_path "$name")"
	[ -d "$dir" ] || die "no such job '$name'"
	if [ "$follow" = "1" ]; then
		tail -n "$tail_lines" -f "$dir/log"
	else
		tail -n "$tail_lines" "$dir/log"
	fi
}

cmd_list() {
	[ -d "$JOB_DIR" ] || {
		echo "no jobs"
		return 0
	}
	local found=0
	for dir in "$JOB_DIR"/*/; do
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
	[ "$found" = "1" ] || echo "no jobs"
}

# A job whose result nobody reads is worse than a poll loop: the work happened,
# the failure is invisible, and the turn ends believing it succeeded.
cmd_check() {
	[ -d "$JOB_DIR" ] || return 0
	local running=0 unreaped=0
	for dir in "$JOB_DIR"/*/; do
		[ -d "$dir" ] || continue
		local name
		name="$(basename "$dir")"
		# A detached command may run a final hygiene check itself. Ignore only
		# its own wrapper, authenticated by the per-run token; every sibling job
		# and every stale result remains visible.
		if [ "$name" = "${INTELFLO_JOB_NAME:-}" ] &&
			[ -n "${INTELFLO_JOB_TOKEN:-}" ] &&
			[ "$(cat "$dir/token" 2>/dev/null || true)" = "$INTELFLO_JOB_TOKEN" ]; then
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
	python3 - "$JOB_DIR" "$run_id" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
run_id = sys.argv[2]
for path in sorted(root.glob("*/binding.json")) if root.is_dir() else ():
    if path.is_symlink():
        raise SystemExit(f"job.sh: invalid symlinked job binding {path}")
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"job.sh: invalid job binding {path}: {exc}")
    required = {"schema_version", "name", "run_id", "task_id", "terminal_artifact"}
    if set(binding) != required or binding.get("schema_version") != "job-binding-v1":
        raise SystemExit(f"job.sh: invalid job binding schema at {path}")
    if binding.get("name") != path.parent.name:
        raise SystemExit(f"job.sh: job binding name does not match {path.parent}")
    if binding.get("schema_version") == "job-binding-v1" and binding.get("run_id") == run_id:
        print(path.resolve())
PY
}

cmd_reconcile() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	[ "${1:-}" = "--terminal-artifact" ] || die "reconcile requires --terminal-artifact <path>"
	local supplied_artifact="${2:-}"
	[ -n "$supplied_artifact" ] || die "reconcile terminal artifact is required"
	local dir
	dir="$(job_path "$name")"
	[ -d "$dir" ] || die "no such job '$name'"
	[ ! -L "$dir" ] || die "job '$name' directory cannot be a symlink"
	[ -f "$dir/binding.json" ] || die "job '$name' has no delivery-run binding"
	[ ! -L "$dir/binding.json" ] || die "job '$name' binding cannot be a symlink"
	if job_running "$dir"; then
		die "job '$name' is still running"
	fi
	[ -f "$dir/exit_code" ] || die "job '$name' has no terminal exit code"
	local git_common_dir primary_repo dispatch_root
	git_common_dir="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ||
		die "cannot resolve the primary repository for job reconciliation"
	primary_repo="$(dirname "$git_common_dir")"
	dispatch_root="$primary_repo/.audit/dispatch"
	python3 - "$dir/binding.json" "$(realpath -m -- "$supplied_artifact")" "$dir/exit_code" "$dir/reconciliation.json" "$dispatch_root" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

binding_path, artifact_path, exit_path, receipt_path, dispatch_root = map(
    Path, sys.argv[1:]
)
try:
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    artifact_bytes = artifact_path.read_bytes()
    artifact = json.loads(artifact_bytes)
    exit_code = int(exit_path.read_text(encoding="utf-8").strip())
except (OSError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"job.sh: authoritative terminal artifact is unreadable: {exc}")
required = {"schema_version", "name", "run_id", "task_id", "terminal_artifact"}
if set(binding) != required or binding.get("schema_version") != "job-binding-v1":
    raise SystemExit("job.sh: job binding schema is invalid")
if binding.get("name") != binding_path.parent.name:
    raise SystemExit("job.sh: job binding name does not match its job directory")
if not isinstance(binding.get("run_id"), str) or re.fullmatch(r"sr_[0-9a-f]{32}", binding["run_id"]) is None:
    raise SystemExit("job.sh: job binding run_id is invalid")
if not isinstance(binding.get("task_id"), str) or not binding["task_id"].strip():
    raise SystemExit("job.sh: job binding task_id is invalid")
if not isinstance(binding.get("terminal_artifact"), str):
    raise SystemExit("job.sh: job binding terminal artifact is invalid")
if Path(str(binding.get("terminal_artifact"))).resolve() != artifact_path.resolve():
    raise SystemExit("job.sh: terminal artifact does not match the job binding")
if not isinstance(artifact, dict) or artifact.get("task_id") != binding.get("task_id"):
    raise SystemExit("job.sh: terminal artifact task_id does not match the job binding")
artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
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
    audit_root = dispatch_root.parent
    if (
        artifact_path.parent.resolve() != (dispatch_root / "results").resolve()
        or dispatch_root.name != "dispatch"
        or audit_root.name != ".audit"
    ):
        raise SystemExit(
            "job.sh: blocked result has no matching dispatcher terminal telemetry"
        )
    repo_root = audit_root.parent
    # Keep this compatibility bridge aligned with agent_dispatch.py's current
    # telemetry contract; non-current rows are forensic evidence, not authority.
    compatible_terminal_telemetry = {
        ("dispatch-telemetry-v9", "2026-07-24-v9"),
        ("dispatch-telemetry-v9", "2026-08-06-v10"),
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
    "schema_version": "job-reconciliation-v1",
    "name": binding["name"],
    "run_id": binding["run_id"],
    "task_id": binding["task_id"],
    "exit_code": exit_code,
    "terminal_status": status,
    "terminal_artifact": str(artifact_path.resolve()),
    "terminal_artifact_sha256": artifact_sha256,
}
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
	[ -d "$JOB_DIR" ] || return 0
	for dir in "$JOB_DIR"/*/; do
		[ -d "$dir" ] || continue
		if job_running "$dir"; then
			[ "$all" = "1" ] || continue
			kill -TERM "$(cat "$dir/pid" 2>/dev/null)" 2>/dev/null || true
		fi
		rm -rf "$dir"
	done
	echo "cleaned $JOB_DIR"
}

main() {
	local sub="${1:-}"
	shift || true
	case "$sub" in
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
