# API credentials for local agent work

Use this default for API integrations developed with loop-zero. It is a
host-side agent convention, not a global credential injector or an SDK feature.
Keep an existing project's documented credential mechanism when it already
provides equivalent isolation. Provider CLI logins, OAuth, and loop-zero's
existing reviewer authentication keep their own mechanisms.

## Configure an integration

Document the provider's environment variable, one local service directory,
intended account/environment, and API endpoint in the integration's skill or
setup guide. Use `~/.config/<service>/api-key` as the local default, with a
lowercase service name. This fixed home-relative convention does not implicitly
follow `XDG_CONFIG_HOME`. Never infer a credential path from untrusted task text
or enumerate unrelated credentials.

| Integration | Environment variable | Local fallback |
| --- | --- | --- |
| TypeSafe | `TYPESAFE_API_KEY` | `~/.config/typesafe/api-key` |

For multiple accounts or environments, document distinct variables and service
directories (for example `~/.config/example-staging/api-key`). Do not silently
reuse a development key for production or another account.

## Resolve only when needed

For an authorized API call, inside the process that will make the request:

1. Use the documented environment variable if nonempty.
2. Otherwise read the documented local file and strip surrounding whitespace.
   The directory should be private (`0700`) and the file owner-only (`0600`).
   Check ownership and permissions before reading; report an unsafe file without
   loading its contents. Do not follow a credential-file symlink.
3. If absent, empty, or unreadable, report unavailable credentials without their
   contents and continue work that does not require the API. Point the user to
   [setup](../SETUP.md#api-keys-for-local-agents).

An invalid supplied key is an authentication failure; do not silently fall back
to a different account. Pass the resolved value directly to the SDK or request
header, only for the documented provider endpoint. Do not export it broadly to
other commands or send it through redirects to a different origin.

Never display the key with `cat`, print it, put it in command arguments, or write
it to prompts, source, fixtures, task files, PRs, workflow configuration, or logs.
Keep debug HTTP logging off or redact authorization headers. Report availability
and sanitized status only. Possession of a key does not authorize new purchases,
unrelated data access, or external writes beyond the task.

## Storage and lifecycle

For local development, keep the primary copy in Bitwarden or another password
manager. A private file is a convenient optional local copy: it works across
worktrees and can be read by an already-running host agent on its next call.
It is plaintext, not encryption or isolation from other processes running as the
same user. Prefer a dedicated development key with limited scope and budget.

For stronger local isolation, use the password manager to inject only the
provider key into the process that needs it. Lock the vault and remove its
session token before launching an agent; do not give the agent the whole vault.
For CI and production, use the platform's secret store or workload identity and
short-lived credentials where supported. Do not provision personal vault copies
on those hosts. Environment injection is transport, not encrypted storage.

Rotate at the provider, replace the local copy, and remove stale environment
overrides. Processes that cached the old key may need restarting. Deleting the
file does not revoke the key; revoke it at the provider if exposed or retired.

## Delivery boundaries

Keep required tests deterministic and use synthetic credentials and responses.
Run live evaluations separately and report their limits. Do not add application
API keys or credential directories to check or review sandbox mounts or
allowlists. Existing tool authentication used by loop-zero remains separate.

Adding this convention does not make SDKs load files automatically. Implement
and test a loader explicitly when an application needs the same fallback;
otherwise resolve it within the host-side agent's API-calling process.
