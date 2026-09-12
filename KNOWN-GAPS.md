# Known gaps

- The Codex CLI backup transport still bootstraps its isolated configuration
  through the `debug.config_lockfile` export that Codex `0.154.0` removed, so
  its launch readiness reports `config-bootstrap` on the pinned CLI.  Live
  conformance runs the preferred app-server transport, whose protected
  credential never permits a CLI fallback, so the gap is currently latent.
