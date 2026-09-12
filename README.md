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
02:17 UTC and by manual dispatch.  Its three independent jobs install and
verify Claude Code `2.1.269`, Codex CLI and Python SDK `0.154.0`, and Cursor
Agent `2026.09.08-6caf4ff`.  Each job then explicitly invokes the otherwise
uncollected live suite for success, malformed adapter input, permission denial,
cancellation, child disconnect, restart/resume, and timeout.  The validation
shell follows the ordinary `checks.yml` bubblewrap layout, but deliberately
retains network access for these jobs.  Every adapter child is nested in its
own mandatory filesystem wrapper; the child environment allowlist and sealed
credential boundary remain active.

Configure these Actions repository secrets with the native subscription-login
credential JSON expected by the corresponding runner broker:

- `LOOPZERO_CONFORMANCE_CLAUDE_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CODEX_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CURSOR_CREDENTIAL`

A missing secret is a hard job failure.  The workflow writes it with mode 0600
only below `RUNNER_TEMP`, binds it at a fixed read-only path in the outer
sandbox, passes it through the runner's brokered descriptor seam, removes it at
step exit, and never prints it or places it in the checkout or artifacts.

The optional repository variable `LOOPZERO_CONFORMANCE_BUDGET_USD` is the
aggregate known-cost ceiling per runtime and defaults to USD 2.  Per-run token,
turn, and dollar caps are independent `RuntimeSettings.budget` values.  Claude
receives its native max-turn, task-token, and USD options; Codex runs exactly
one SDK turn and receives its native `output_token_limit`; Cursor has no
corresponding flag in the pinned CLI.  All adapters still enforce observed
token/USD overruns after return.  Reporting and ceiling decisions use the
package's frozen [`2026-09-12 price table`](src/loopzero/runners/pricing.py),
not a live price lookup.  Missing token evidence produces `cost_usd = null`
and fails that live scenario's accounting requirement; it is never counted as
zero.  Each job uploads only normalized result fields and adds per-scenario
outcomes and known costs to the Actions summary.

Gate A-G2 means two consecutive **scheduled** UTC nights on the default branch
are green for all three runtime jobs.  Both nights must have artifacts showing
the exact pins, all seven passing scenarios, known aggregate cost within the
configured ceiling, and no accounting gaps.  A manual dispatch, missing night,
skip, cancellation, replay-only run, absent artifact, unknown cost, or failed
runtime cannot substitute for either green night.
