# Runner conformance

The default suite runs all seven scenarios against the deterministic fake
vendor. Set `LOOPZERO_CONFORMANCE_RUNNER=claude|codex|cursor` to replay the same
scenarios through one native adapter.

Real-vendor replay proves request serialization, normalized response parsing,
and cancellation dispatch only. It does not exercise a live SDK, credentials,
the filesystem sandbox, or a paid vendor session. The nightly real-runtime job
tracked by issue #12 is the end-to-end proof for those integrations.

The opt-in suite is
`tests/conformance/live/live_conformance.py`.  Its filename intentionally does
not match default pytest discovery.  Select one or more runtimes with a
comma-separated `LOOPZERO_LIVE_RUNTIMES=claude,codex,cursor`, provide the
explicit pinned executable and broker-readable credential path, then name the
file on the pytest command line.  The GitHub workflow is the reference launch:
it supplies the required nested bubblewrap wrapper, an external private state
root and suite-scoped resume home, hard scenario timeouts, normalized JSON
output, and conservative aggregate spend control. Every started invocation
without known usage is charged Claude's USD cap, Codex's output cap plus a
one-token-per-prompt-byte input estimate, or Cursor's fixed USD 0.10 amount. A
suite stops after three killed or three unaccounted runs by default. Cursor
schema-dependent success and resume evidence is explicitly unsupported rather
than decoded from free text. Offline tests verify home/argument plumbing,
while actual provider resume is established only by the nightly artifact. Do
not replace the wrapper with the process layer's unsandboxed test exception
for a live run.
