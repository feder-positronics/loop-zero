# Single declared native account

A trusted host launcher can supply an explicit native account policy through
`RuntimeSettings.from_profile(profile)` or `RuntimeSettings(accounts=...)`.
An absent `[accounts]` table preserves legacy discovery. A present empty table
denies every native vendor; a present table denies vendors it does not name.

```toml
[accounts.codex.primary]
kind = "oauth-login"
credential_path = "/private/credentials/codex.json"
```

Each supported vendor (`claude`, `codex`, `cursor`) accepts exactly one account.
The only fields are `kind` and `credential_path`. Claude supports `oauth-login`
and `setup-token`; Codex and Cursor support `oauth-login`. API keys, selectors,
aliases, multiple accounts, and unknown fields are rejected. The label is a
host configuration label, not an attestation of the identity consuming a run.
Declarations are copied into immutable settings. Profile parsing validates the
shape without reading credentials; admission validates the actual source bytes.

The source must be an absolute, nonsymlink, singly linked 0600 file owned by the
host user in a private 0700 directory. It must be outside repositories, runtime
state, tooling, and requested read/evidence/write roots. Existing native brokers
receive only that explicit source; Claude ambient token fallback is disabled.
Renewal uses the existing package host-refresh sandbox wrapper; if its runtime
mounts or executable are unavailable, renewal fails closed. An executable or
toolchain outside its system/interpreter mounts may require the helper's existing
`LOOPZERO_LIVE_RUNTIME_ROOT` configuration; this feature does not widen those
mounts. It does not borrow
the worker wrapper or discover another account. Source or kind failures raise a content-free `DeclaredAccountError` before
readiness activity. No source path or account label is serialized to children.

Direct adapter `probe`, `run`, and transport-readiness calls establish a
context-local broker scope. Nested calls reuse the held snapshot. Independent
calls and threads own independent scopes and private homes; no process-global
environment changes are made. Every consuming bridge launch receives a fresh
read description with its own offset and lifetime, so readiness cannot consume
the run's snapshot. The host closes its launch descriptor after spawning and
scrubs the private home when the scope ends.

Declared Claude and Codex calls support their protected SDK routes only.
Explicit CLI requests and CLI fallback are denied. Cursor materializes the
selected access-only record in its private CLI home and removes it afterward.
Operational version/import probes receive no account descriptor. Existing
cost, budget and billing-mode semantics are unchanged; this policy adds no
billing or account-consumption assertion.

The parent applies its credential descriptor, private home, and child settings
after caller environment overlays, removing ambient credential/configuration
variables and caller credential descriptors. The bridge receives the trusted
`--require-brokered-credential` argument and rejects missing, false, mismatched,
or invalid credential context before entering vendor code. Its vendor field is
only a narrowing latch: an environment string does not authorize ambient
fallback. The gate accepts existing broker sealed descriptors and their
unlinked read-only 0400 fallback on interpreters without memfd support.

The existing host-supplied sandbox wrapper remains responsible for positive
filesystem containment, including removing ambient credential homes and other
host sources. A nonempty wrapper is not proof of its mount policy. Runtime
settings, executable paths, injected process functions, and that wrapper are
trusted host inputs; request/environment overlays are not credential authority.
Declared launches reject the unsandboxed escape hatch. This slice does not add
rotation, quota accounting, signed account attribution, or a workflow controller.
Consumers still need to adopt the settings and containment contract.
