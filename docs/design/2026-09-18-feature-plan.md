# Feature plan after the v2 rewrite

Date: 2026-09-18. Owner: Marcin. Status: proposed, awaiting owner selection.

## Standard

loop-zero stays lightweight, debuggable and maintainable. A feature enters
only when it names a concrete failure that happens today, the fix reuses an
existing mechanism, and the source budget in [REWRITE-SPEC.md](../REWRITE-SPEC.md)
(2,500 lines, 2,042 used) absorbs it. "The old engine had it" is not a reason.

## Method

The v2 tree was compared against the mechanisms of the retired workflow
engine (git history before `8f0fcb18`), then the shortlist was reviewed by
Codex `gpt-6-astra` with the source open. Its critique corrected two cost
estimates, dropped one item and added three narrow fixes. Each surviving
change has its own design note in this directory.

## Selected changes, in order

| # | Change | Failure prevented | Est. lines | Issue |
| --- | --- | --- | --- | --- |
| 1 | [Checks and readiness config from the base revision](2026-09-18-base-revision-config.md) | A PR edits `workflow.toml` to weaken its own checks or `required_ci` | 30–60 | [#128](https://github.com/feder-positronics/loop-zero/issues/128) |
| 2 | [Reviewers run inside bwrap](2026-09-18-reviewer-sandbox.md) | Reviewer CLI reads the whole host home; isolation is only CLI flags | 80–150 | [#129](https://github.com/feder-positronics/loop-zero/issues/129) |
| 3 | [Re-sample HEAD after long runs](2026-09-18-post-run-drift-recheck.md) | Check report or review is saved for a HEAD that changed during the run | 15–25 | [#130](https://github.com/feder-positronics/loop-zero/issues/130) |
| 4 | [PR base and remote branch reconciliation](2026-09-18-pr-base-and-branch-cleanup.md) | Review against one base, merge into another; remote branch left after a failed cleanup | 15–25 | [#131](https://github.com/feder-positronics/loop-zero/issues/131) |
| 5 | [Model and duration in the review header](2026-09-18-review-header-provenance.md) | A posted review cannot be traced to the model that produced it | 15–30 | [#132](https://github.com/feder-positronics/loop-zero/issues/132) |

Total expected growth is under 250 source lines, within budget.

## Rejected

- **Worktree locks.** Git refuses two checkouts of one branch. Two processes
  in the *same* worktree can still race; change 3 catches the harmful case
  (evidence saved for a moved HEAD) without a lock file.
- **Preflight or `doctor` command.** Every command fails on first use with a
  typed reason, and a failed reviewer run does not consume the review budget.
  A preflight duplicates those checks and can still be stale at use time.
- **Path-based mandatory security review.** Path classification is unreliable
  and competes with the one-review budget. The `security-review` skill and a
  sentence in `implement` cover it at zero code.
- **Retry, recovery or attempt framework.** `pr` updates an existing PR,
  `merge` recognises an already merged PR, `review --repost` covers the
  crash between model output and posting. Change 4 closes the one remaining
  gap without new state.
- **Diagnostics command, token accounting, `RunnerTimeout` class.** Timing
  goes into the review header (change 5). Timeouts already fail clearly and
  fall through to the next family. No caller needs to distinguish them.

## Sequencing

One PR per change, each with its tests and a one-line budget note. Change 1
ships alone because consumers see a behaviour change. Changes 3 and 4 are
independent and small; either can go first. Change 2 is last because it is
the largest and its tests need the fake `bwrap` fixture to pass a prompt
through.
