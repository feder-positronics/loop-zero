# Runner conformance

The default suite runs all seven scenarios against the deterministic fake
vendor. Set `LOOPZERO_CONFORMANCE_RUNNER=claude|codex|cursor` to replay the same
scenarios through one native adapter.

Real-vendor replay proves request serialization, normalized response parsing,
and cancellation dispatch only. It does not exercise a live SDK, credentials,
the filesystem sandbox, or a paid vendor session. The nightly real-runtime job
tracked by issue #12 is the end-to-end proof for those integrations.
