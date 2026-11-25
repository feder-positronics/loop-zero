The synthetic five- and seven-read streams model Codex 0.165.0 JSONL. Each
includes a matched start and completion for the anchored head and diff reads,
then unrelated status reads and a completed approval. The invented diff and
head match the recorded review context. The streams contain no configured or
reported model or effort.

Codex rust-v0.165.0 (`360319d8dee62d87e2d59fc0c21805d79519cfed`)
defines `ThreadStartedEvent` with only `thread_id` in
`codex-rs/exec/src/exec_events.rs`. The JSONL processor emits that event for
`print_config_summary`; the human settings banner is absent from JSON output.
