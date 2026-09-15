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

The Codex and Cursor environment secrets remain their native subscription-login
credential JSON:

- `LOOPZERO_CONFORMANCE_CODEX_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CURSOR_CREDENTIAL`
