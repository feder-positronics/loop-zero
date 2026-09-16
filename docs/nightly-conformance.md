# Nightly conformance credentials

The `nightly-conformance` GitHub environment holds one secret per runtime. Use
a dedicated CI account where possible. In particular,
`LOOPZERO_CONFORMANCE_CLAUDE_CREDENTIAL` must be the single-line setup token
produced by `claude setup-token` (`sk-ant-oat01-...`), optionally ending in one
newline. Do not store Claude browser-login OAuth JSON in that secret: its
refresh token rotates on use, while a GitHub Actions job cannot write the
replacement back to the secret for the next run.

The trusted host seal identifies Claude inputs by content, validates the setup
token remotely, and writes this compact JSON snapshot:

```json
{"claudeCodeOauthToken":"<validated setup token>","source":"setup-token-file"}
```

That object contains access material only and no refresh capability. The source
must be a regular, owner-only mode-0600 file. The snapshot is mode 0600 in its
mode-0700 staging directory, the source is removed before checked-out code
starts, and only the snapshot is mounted read-only in the validation bubble.
Explicit Claude OAuth files remain supported for operator-owned hosts where the
CLI can safely persist rotation; their sealing and refresh path is unchanged.

## Codex host renewal

`LOOPZERO_CONFORMANCE_CODEX_CREDENTIAL` must contain an **access-only snapshot**,
never the host's native login file. The snapshot retains the native JSON shape
for CLI compatibility, but its `refresh_token` field contains the access token;
it cannot refresh. The owning host keeps the real refresh token, and the existing
broker asks the pinned vendor CLI to refresh and persist rotation there.

From an installed, reviewed loopzero wheel with the `codex` extra, run:

```sh
umask 077
stage=$(mktemp -d "$XDG_RUNTIME_DIR/loopzero-renew.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
loopzero accounts renew ci --source "$HOME/.codex/auth.json" --out "$stage/snapshot.json"
gh secret set LOOPZERO_CONFORMANCE_CODEX_CREDENTIAL \
  --repo feder-positronics/loop-zero --env nightly-conformance < "$stage/snapshot.json"
```

The source must be an explicit absolute path to a regular mode-0600 file outside
repositories. The output must be new, in an owner-only mode-0700 directory outside
repositories. Neither credential is printed. `ci` is an operator label only;
this command does not yet resolve declared accounts or produce a signed account
alias (credential harness steps 3–4). No ambient credential discovery occurs.

Renewal demands 72 hours of remaining access-token validity plus the broker's
five-minute safety margin. It refreshes only below that horizon. A revoked login,
an insufficient refreshed lifetime, or a short-lived access-only source fails
closed, leaving no new snapshot to upload. A sufficiently fresh host login is
sealed without a network call. The broker still serializes refresh and verifies
the account and source before installing the vendor's replacement.

### User systemd timer

Reviewed examples are in [examples/renew-codex-nightly.sh](examples/renew-codex-nightly.sh),
[examples/loopzero-codex-renew.service](examples/loopzero-codex-renew.service), and
[examples/loopzero-codex-renew.timer](examples/loopzero-codex-renew.timer).

1. Install the reviewed wheel and its `codex` extra in a dedicated environment
   outside any checkout. Use Python 3.12 or later, either a system installation
   under `/usr` or a managed CPython installation (for example, uv). Venvs with
   copied or symlinked executables retain their SDK imports during refresh:

   ```sh
   /usr/bin/python3 -m venv --without-pip "$HOME/.local/share/loopzero-renew"
   uv pip install --python "$HOME/.local/share/loopzero-renew/bin/python" \
     '/absolute/path/to/loopzero-VERSION-py3-none-any.whl[codex]'
   ```

   Replace the wheel placeholder with a wheel built from the reviewed renewal
   commit; the released v0.4.3 wheel does not contain this command. The refresh
   command needs Linux user namespaces and `bwrap`. To use managed Python,
   replace the venv creation command with
   `uv venv --python 3.12 "$HOME/.local/share/loopzero-renew"`.
   The trusted sealer preserves the venv interpreter identity and mounts its
   environment plus the running Python's `sys.base_prefix` and
   `sys.base_exec_prefix` read-only. This exposes the selected installation's
   standard library and extension modules without mounting its shared managed
   runtime directory or the host home. Keep the venv and its base installation
   available together; install SDKs in that venv.
2. Authenticate `gh` on this host with permission to update the repository's
   `nightly-conformance` environment secret. Use a dedicated CI vendor account
   where possible; keep its native login on this host only.
3. Copy the script to `$HOME/.local/libexec/renew-codex-nightly.sh` and the two
   units to `$HOME/.config/systemd/user/`. Edit `ExecStart` for the installed
   loopzero and gh executable paths, source login, repository, environment and
   secret. These arguments contain paths and labels only, never token material.
4. Run `systemctl --user daemon-reload`, then
   `systemctl --user start loopzero-codex-renew.service`. This uploads a snapshot;
   check `systemctl --user status loopzero-codex-renew.service` for success.
5. Enable scheduling with
   `systemctl --user enable --now loopzero-codex-renew.timer`. For unattended
   operation while logged out, configure user lingering with the host operator.

The timer runs hourly with up to five minutes of jitter and catches a missed run
when the user manager restarts. The script creates a private staging directory
under `XDG_RUNTIME_DIR`, uploads only `snapshot.json` on stdin, suppresses provider
output, and removes the staging directory on success, failure or a catchable
termination signal. During refresh, the broker also uses a private state directory
inside that staging directory; it can temporarily contain a refresh-capable
rotated login. This state stays on the owning host and is never uploaded.
An upload failure leaves the previous GitHub secret unchanged; the next hourly
run retries. Monitor failed services: suspension, a revoked login, or more than
72 hours without a successful renewal can leave the nightly without a usable
credential. A forced kill during refresh can leave refresh-capable material in
the private host state directory until the runtime directory is removed. Never
archive or upload the staging directory; only the validated snapshot is suitable
for GitHub.

If the vendor returns an access token too short for the 72-hour horizon, renewal
persists the validated same-account rotated login on the host, then refuses to
export it. The next scheduled run uses the replacement refresh token.

Before contacting the vendor, the broker writes and fsyncs a private
`.auth.json.refresh-pending` file beside the source login. It contains only a
SHA-256 fingerprint of the refresh token, never the token itself. Successful
durable installation clears it. Timeout, interrupted refresh, invalid vendor
output, or failed installation retains it: subsequent runs refuse to reuse the
same refresh token, even after a restart or access-expiry edit. This is an
indefinite backoff until recovery, not an hourly vendor retry. Obtain a new host
login with a different refresh token and rerun renewal; the broker automatically
clears the old marker. Do not delete the marker to retry an uncertain token.
Malformed or unsafe marker files fail closed and require host operator repair.
A failure before vendor launch can conservatively require the same recovery.

The broker checks account consistency using only the recognized
`https://api.openai.com/auth.chatgpt_account_id` claim inside each decoded access
or identity JWT: a present claim must be a nonempty string equal to outer
`tokens.account_id`. Missing or null namespaces/claims remain unknown; malformed
namespace or claim types are rejected. `sub`, email, `chatgpt_user_id`, `user_id`,
and top-level account-like fields do not substitute for this claim. This matches
the account namespace in the [pinned vendor parser](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/login/src/token_data.rs).

Access tokens retain the JWT and expiry requirement. An identity token with
exactly three dot-separated segments must decode to a valid JSON object; other
nonempty identity strings remain opaque for compatibility, including strings
with one or more than two dots. Claims are decoded without signature
verification, solely to reject internal disagreement. Passing these checks does
not authenticate an account, establish provenance, or authorize an alias.
Inconsistent source credentials are rejected before refresh; inconsistent vendor
replacements are never installed or exported, and retain the pending marker
requiring host recovery described above.

The workflow continues to validate and seal the snapshot before launching
checked-out code. It fails if the access token no longer covers the job; it
cannot refresh on GitHub.

### Cursor

Cursor remains non-blocking under #35. Its host-renewal mechanism is not provided
by this Codex command; do not use it to upload a Cursor native login containing
refresh capability. Completing Cursor renewal remains part of #32 step 2.

## Cost presentation and budget accounting

The nightly summary reports **API-equivalent USD**, a usage proxy. Under a
subscription this is not a per-turn payment or a conversion of subscription
quota into dollars. Vendor-reported or estimated values retain their
`cost_source`; billing mode is not inferred from those values.

Normalized artifacts retain the existing `cost_usd` and `charged_cost_usd` keys
for compatibility. `cost_usd` is the runtime's API-equivalent usage value when
known. Scenario and aggregate `charged_cost_usd` values are **budget debits**:
they include conservative fallback or vendor-cap charges for killed or
unaccounted invocations and can exceed known usage. Read `accounting` and
`cost_sources` alongside each scenario's debit. The aggregate sums those debits;
it does not report money paid.

The configured USD ceiling and all existing charge, kill, and unknown-cost
limits continue to enforce the same runaway bound. Credential-derived billing
mode and account/model quota evidence remain follow-up work in #34 and #32.
