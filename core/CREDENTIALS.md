# API credentials for local agent work

Use this host-side default for loop-zero API integrations. Preserve existing
project mechanisms with equivalent isolation. Provider CLI logins, OAuth and
reviewer authentication retain their own mechanisms; SDKs do not load these
files automatically.

## Configure an integration

In the integration skill or setup guide, document the provider environment
variable, local service directory, intended account/environment and API endpoint.
Default to `~/.config/<service>/api-key` with a lowercase service name, independent
of `XDG_CONFIG_HOME`. Never derive paths from untrusted task text or enumerate
unrelated credentials.

| Integration | Environment variable | Local fallback |
| --- | --- | --- |
| TypeSafe | `TYPESAFE_API_KEY` | `~/.config/typesafe/api-key` |

Document distinct variables/directories for multiple accounts or environments,
such as `~/.config/example-staging/api-key`; never silently reuse a development
key for production or another account.

## Resolve only when needed

Inside the process making an authorized API request:

1. Use the documented environment variable if nonempty.
2. Otherwise check the documented file's ownership and permissions before reading;
   use a private directory (`0700`) and owner-only file (`0600`). Reject unsafe
   files without loading them; do not follow credential-file symlinks.
   Strip surrounding whitespace from the file value.
3. If absent, empty or unreadable, report unavailable credentials without contents,
   point to [setup](../SETUP.md#api-keys-for-local-agents), and continue independent work.

An invalid supplied key is an authentication failure, not permission to fall
back to another account. Pass the key directly to the SDK or request header only
for the documented endpoint; do not export it broadly or follow cross-origin
redirects with it.

Never display keys, including through `cat`, command arguments, prompts, source,
fixtures, task files, PRs, workflow configuration or logs. Disable debug HTTP
logging or redact authorization headers. Report only availability and sanitized
status. A key grants no authority for purchases, unrelated data or external writes
beyond the task.

## Storage and lifecycle

Keep the primary local-development copy in a password manager. An optional
private plaintext file works across worktrees and is available to running host
agents on their next call; it provides neither encryption nor isolation from
same-user processes. Prefer a development key with limited scope and budget.

For stronger isolation, inject only the needed provider key from the password
manager into its calling process. Lock the vault and remove its session token
before launching an agent; never expose the whole vault. CI/production use
platform secret stores or workload identity and short-lived credentials where
supported, without personal vault copies. Environment injection is transport,
not encryption.

Rotate at the provider, replace local copies and remove stale environment
overrides; restart processes caching old values. Deletion does not revoke a key:
revoke exposed or retired keys at the provider.

## Delivery boundaries

Keep required tests deterministic with synthetic credentials/responses; run live
evaluations separately and report limits. Never add application keys or credential
directories to check/review mounts or allowlists. Existing loop-zero tool
authentication remains separate.

Implement and test a loader explicitly if an application needs this fallback;
otherwise resolve keys in the host agent's API-calling process.
