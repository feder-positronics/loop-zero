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
#   scripts/util/job.sh start <name> -- <command...>
#   scripts/util/job.sh wait  <name> [--timeout SECONDS] [--tail LINES]
#   scripts/util/job.sh run   <name> [--timeout SECONDS] [--tail LINES] -- <command...>
#   scripts/util/job.sh status <name>
#   scripts/util/job.sh log   <name> [--tail LINES] [--follow]
#   scripts/util/job.sh list
#   scripts/util/job.sh check                 # non-zero if any job is running or unreaped
#   scripts/util/job.sh clean [--all]
#
# `wait` exits with the job's own exit code, 124 on timeout, 2 on usage error.
# Jobs live under $INTELFLO_JOB_DIR (default .pid/jobs/) inside the worktree, so
# parallel worktrees never collide.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOB_DIR="${INTELFLO_JOB_DIR:-$REPO_ROOT/.pid/jobs}"
DEFAULT_TIMEOUT="${INTELFLO_JOB_TIMEOUT:-1800}"
DEFAULT_TAIL=40

usage() {
	sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
	exit "${1:-2}"
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
	[ "${1:-}" = "--" ] || die "expected '--' before the command"
	shift
	[ "$#" -gt 0 ] || die "no command given"

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

	# setsid detaches from the agent's shell session, so the job survives the
	# exec_command timeout that would otherwise orphan or kill it.
	setsid bash -c '
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
	local timeout="$DEFAULT_TIMEOUT" tail_lines="$DEFAULT_TAIL"
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
		*) die "unknown option '$1'" ;;
		esac
	done
	case "$timeout" in '' | *[!0-9]*) die "--timeout must be a whole number of seconds" ;; esac
	case "$tail_lines" in '' | *[!0-9]*) die "--tail must be a whole number of lines" ;; esac

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

cmd_run() {
	local name="${1:-}"
	shift || true
	validate_name "$name"
	local timeout="$DEFAULT_TIMEOUT" tail_lines="$DEFAULT_TAIL"
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
		*) die "unknown option '$1'" ;;
		esac
	done
	[ "${1:-}" = "--" ] || die "expected '--' before the command"
	shift
	cmd_start "$name" -- "$@" >/dev/null || return $?
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
		if job_running "$dir"; then
			running=$((running + 1))
			echo "job '$name' is still RUNNING (pid $(cat "$dir/pid" 2>/dev/null))" >&2
		elif [ ! -f "$dir/waited" ]; then
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
	run) cmd_run "$@" ;;
	status) cmd_status "$@" ;;
	log) cmd_log "$@" ;;
	list) cmd_list "$@" ;;
	check) cmd_check "$@" ;;
	clean) cmd_clean "$@" ;;
	-h | --help | help) usage 0 ;;
	*) usage 2 ;;
	esac
}

main "$@"
