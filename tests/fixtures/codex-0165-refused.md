These fixtures derive from the refused native receipts for IntelFlo #9284 and #9275.
Original receipt SHA256:

- #9284: `aec49ec7aee382f410c48f8a58887de18249fc22b50bbd06c2d0621da0d88396`
- #9275: `755aee81eca0c0a103cf4d641ac3779345b139ca168b897f2385787274e104bd`

Private paths/session IDs are replaced. Source outputs, extra inspection commands
and verdict prose are redacted; the head/diff command shapes, event ordering,
completion statuses, token counts and absent reported settings are retained.
The diff is synthetic and its byte/hash context is recomputed. No configured
settings were added to these historical streams.

Codex rust-v0.165.0 (`360319d8dee62d87e2d59fc0c21805d79519cfed`)
`codex-rs/exec/src/exec_events.rs` defines `ThreadStartedEvent` with only
`thread_id`. `event_processor_with_jsonl_output.rs` lines 403–406 and 605–612
emit only that event for `print_config_summary`; the human settings banner is
not emitted in JSON mode. There are no renamed model/effort fields to recover.
