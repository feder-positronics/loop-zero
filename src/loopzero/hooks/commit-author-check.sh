#!/bin/bash
# Ensure local commits use the Vercel-authorized GitHub identity.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../kernel/settings.sh"

if [ "${LOOPZERO_ENV_PREFIX:-LOOPZERO}" = "INTELFLO" ]; then
    default_name="eowca"
    default_email="4008965+eowca@users.noreply.github.com"
else
    default_name=""
    default_email=""
fi
EXPECTED_NAME="$(loopzero_env COMMIT_AUTHOR_NAME "$default_name")"
EXPECTED_EMAIL="$(loopzero_env COMMIT_AUTHOR_EMAIL "$default_email")"
[ -n "$EXPECTED_NAME" ] && [ -n "$EXPECTED_EMAIL" ] || {
    echo "Commit identity policy is not configured for ${LOOPZERO_ENV_PREFIX:-LOOPZERO}." >&2
    exit 1
}

if [ "$(loopzero_env COMMIT_AUTHOR_BYPASS '')" = "1" ]; then
    echo "commit-author-check bypassed via ${LOOPZERO_ENV_PREFIX:-LOOPZERO}_COMMIT_AUTHOR_BYPASS=1"
    exit 0
fi

extract_name() {
    sed -E 's/^(.*) <[^>]+> [0-9]+ [+-][0-9]+$/\1/'
}

extract_email() {
    sed -E 's/^.*<([^>]+)> [0-9]+ [+-][0-9]+$/\1/'
}

author_ident="$(git var GIT_AUTHOR_IDENT)"
committer_ident="$(git var GIT_COMMITTER_IDENT)"

author_name="$(printf '%s\n' "$author_ident" | extract_name)"
author_email="$(printf '%s\n' "$author_ident" | extract_email)"
committer_name="$(printf '%s\n' "$committer_ident" | extract_name)"
committer_email="$(printf '%s\n' "$committer_ident" | extract_email)"

if [ "$author_name" = "$EXPECTED_NAME" ] \
    && [ "$author_email" = "$EXPECTED_EMAIL" ] \
    && [ "$committer_name" = "$EXPECTED_NAME" ] \
    && [ "$committer_email" = "$EXPECTED_EMAIL" ]; then
    exit 0
fi

cat >&2 <<EOF
Commit identity must use the consumer-approved account.

Expected author/committer:
  $EXPECTED_NAME <$EXPECTED_EMAIL>

Resolved by git:
  author:    $author_name <$author_email>
  committer: $committer_name <$committer_email>

Run /commit-autofix, or commit with:
  GIT_AUTHOR_NAME="$EXPECTED_NAME" \\
  GIT_AUTHOR_EMAIL="$EXPECTED_EMAIL" \\
  GIT_COMMITTER_NAME="$EXPECTED_NAME" \\
  GIT_COMMITTER_EMAIL="$EXPECTED_EMAIL" \\
  git commit -m "your message"
EOF

exit 1
