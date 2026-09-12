# loop-zero

A small, versioned agent workflow shared by repositories. This repository owns
the core; product repositories consume identical pinned snapshots and retain
their own commands, architecture, security and release policy.

The portable distribution has two parts pinned by one commit SHA. The vendored
[`core/`](core/CONTRACT.md) snapshot holds the contract, four skills, one
handoff, thin Codex and Claude entry points, four conditional framework
profiles and read-only pin and check-policy tools. The `loopzero` Python
package, installed from the same SHA, holds the executable mechanisms: the
worktree lease, sandboxed validation children, the signed evidence ledger,
detached jobs, runner adapters, the dispatcher and the delivery state machine,
plus a generator for consumer wiring. The core ships no scheduler service,
dashboard, cloud control plane or installer beyond that generator.

See [setup and revision updates](SETUP.md). Run the source tests with
`.venv/bin/python -m pytest -q` (Python 3.12+ and Git).

A deposit is not evidence of product portability. That requires real changes in
two consumers on the same final core revision, both runtime entry points used,
and the repositories' ordinary validation and review evidence.

Version 0.2.1 fixes release-metadata environment checks and adds check classes,
evidence-backed overrides, and an offline check-policy report. Consumers must
bump their snapshot and full commit pin to the reviewed 0.2.1 merge commit as
described in SETUP.md. The
single durable debt list is [KNOWN-GAPS.md](KNOWN-GAPS.md), curated at closeout.
CI runs the tests and `python3 core/tools/status.py --known-gaps KNOWN-GAPS.md
--check-child-env`; run `git diff --check` before freezing a review candidate.
There are no configured lint, type or complexity ratchets in this source package;
consumers must run every applicable gate from their own contract.

## Nightly real-runtime conformance

[`nightly-conformance.yml`](.github/workflows/nightly-conformance.yml) runs at
02:17 UTC and by manual dispatch from `refs/heads/main` only.  Its three independent jobs install and
verify Claude Code `2.1.269`, Codex CLI and Python SDK `0.154.0`, and Cursor
Agent `2026.09.08-6caf4ff`.  Each job then explicitly invokes the otherwise
uncollected live suite for success, malformed adapter input, permission denial,
cancellation, child disconnect, restart/resume, and timeout.  The validation
shell follows the ordinary `checks.yml` bubblewrap layout, but deliberately
retains network access for these jobs.  Every adapter child is nested in its
own mandatory filesystem wrapper; the child environment allowlist and sealed
credential boundary remain active.

Create a GitHub Actions environment named `nightly-conformance`, restrict its
deployment branches to `main`, and require an environment reviewer to verify
the queued commit before releasing credentials.  Configure these environment
secrets (not repository-level secrets) with the native subscription-login
credential JSON expected by the corresponding runner broker:

- `LOOPZERO_CONFORMANCE_CLAUDE_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CODEX_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CURSOR_CREDENTIAL`

A missing secret is a hard job failure.  The workflow installs its cleanup trap
before writing the mode-0600 source below `RUNNER_TEMP`, binds that source
read-only at a fixed path in the outer sandbox, and removes it at step exit.
Each broker invocation copies the source into a new private state-root
directory, may refresh only that disposable copy, seals an access-only worker
snapshot, and scrubs the copy.  A refreshed credential is deliberately never
written back to the environment-secret source, so scenario-to-scenario drift
cannot accumulate in the job.  If provider rotation invalidates that original
refresh token, an environment owner must re-seal the approved credential; the
worker has no capability to update the environment secret. The source is never printed or placed in the
checkout or artifacts.  Because checked-out conformance code necessarily uses
the credential broker while network is enabled, the environment approval is
also the authorization boundary for the exact commit being exercised.

The optional repository variable `LOOPZERO_CONFORMANCE_BUDGET_USD` is the
aggregate charged-cost ceiling per runtime and defaults to USD 2.  Before each
launch, its remaining allowance narrows the per-run `RuntimeSettings.budget`.
The realistic per-turn output/task cap is 32,768 tokens: Claude receives native
turn, task-output and USD caps, while Codex runs one SDK turn with the pinned
CLI's top-level `output_token_limit`.  That Codex key is present in 0.154.0's
compiled configuration schema and is passed through both CLI and SDK config
layers.  Normalized enforcement compares `RuntimeUsage.output_tokens` with
this output cap; input and cache counters are used for price reporting, not
miscompared with an output-only limit.  There is no separate total-token cap.

Reporting uses the frozen [`2026-09-12 price table`](src/loopzero/runners/pricing.py),
not a live lookup.  A zero-token paid result and incomplete usage remain
unknown, never zero-cost evidence.  If cancellation, disconnect, or timeout
kills Claude or Codex before usage arrives, the aggregate is conservatively
charged the native per-run vendor cap and no further scenario launches once
the charged ceiling is reached.  Cursor exposes no vendor cost/token cap in
the pinned CLI; its only pre-return spend bounds are the 45-second timeout and
finite seven-scenario suite, and its report states `harness-only-unpriced` when
a killed run has no usage.  Each runtime gets one private state-root session
home bound read-write into both restart/resume launches and scrubbed at suite
end.  Each job uploads only normalized fields and reports observed or
conservatively charged accounting separately.

Gate A-G2 means two consecutive **scheduled** UTC nights on the default branch
are green for all three runtime jobs.  Both nights must have artifacts showing
the exact pins (including Cursor's build hash), all seven passing scenarios,
charged aggregate cost within the configured ceiling, and known or conservative
vendor-cap accounting (with Cursor's explicit harness-only exception for killed
runs).  A manual dispatch, missing night,
skip, cancellation, replay-only run, absent artifact, unknown cost, or failed
runtime cannot substitute for either green night.
