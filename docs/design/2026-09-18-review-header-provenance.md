# Model and duration in the review header

Date: 2026-09-18. Status: proposed, trimmed after review.

## Failure today

A posted review header reads `loopzero primary review by claude on <sha>`.
The model that produced it is not recorded anywhere. When a review pattern
looks wrong months later there is no way to tell which model or version was
running, and no way to compare durations between families.

## Change

- `ReviewResult` gains two optional fields: `model: str | None` and
  `duration_s: float | None`. Missing values render as `unknown` and never
  affect the verdict.
- Claude: `model` comes from the result envelope's `modelUsage` keys or the
  configured model name if present; `duration_s` from `_proc.Completed`.
- Codex: the final message carries no envelope. `duration_s` comes from
  `_proc.Completed`; `model` is read from the `model` line in the stderr
  session banner when present, else `unknown`.
- `github._review_body` appends `model <name>, <duration>s` to the header.
- `_save_review` and `_load_review` persist the two fields so `--repost`
  reproduces the same header.

## Not included

Token counts. Only Claude's envelope exposes them reliably; reporting zero
for Codex would be misleading. Revisit when both CLIs expose usage in a
structured form.

## Tests

- Claude fake returns a model name; header contains it.
- Codex fake omits the banner; header prints `unknown` and the verdict is
  unchanged.
- Repost after a fake `gh` failure keeps the same header.

## Cost

15–30 lines across `runners.py`, `github.py`, `cli.py`, three tests.
