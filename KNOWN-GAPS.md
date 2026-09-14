# Known gaps

- The Codex CLI backup transport still bootstraps its isolated configuration through the `debug.config_lockfile` export that Codex 0.154.0 removed, so its launch readiness reports `config-bootstrap` on the pinned CLI; live conformance runs the app-server transport, whose protected credential never permits a CLI fallback, so the gap is latent.
- Nightly conformance: the Cursor job is non-blocking because the only available Cursor login is a Free tier and the owner is holding the paid subscription (issue #35); Cursor readiness is still executed and its normalized result still uploaded, so gate A-G2 is read from the Claude and Codex jobs until a paid login is provided.
