#!/bin/bash
# Installed on the login-owning host; all arguments are paths or public labels.
# Usage: script LOOPZERO SOURCE GH REPOSITORY ENVIRONMENT SECRET
set +x
set -eu
umask 077
fail() { printf '%s\n' "$1" >&2; exit 1; }
[ "$#" -eq 6 ] || fail 'account timer failed: InvalidArguments'
case "$1" in /*) ;; *) fail 'account timer failed: InvalidExecutable';; esac
case "$3" in /*) ;; *) fail 'account timer failed: InvalidExecutable';; esac
[ -n "${XDG_RUNTIME_DIR:-}" ] || fail 'account timer failed: RuntimeDirectoryUnavailable'
stage=$(mktemp -d "$XDG_RUNTIME_DIR/loopzero-renew.XXXXXXXX" 2>/dev/null) ||
    fail 'account timer failed: StagingFailed'
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 1' HUP INT TERM
"$1" accounts renew ci --source "$2" --out "$stage/snapshot.json" >/dev/null 2>&1 ||
    fail 'account timer failed: CredentialRenewalFailed'
# stdin is the access-only output, never the host login. Suppress gh output:
# even a provider failure must not echo a credential into the systemd journal.
"$3" secret set "$6" --repo "$4" --env "$5" < "$stage/snapshot.json" >/dev/null 2>&1 ||
    fail 'account timer failed: CredentialUploadFailed'
printf '%s\n' 'account snapshot uploaded'
