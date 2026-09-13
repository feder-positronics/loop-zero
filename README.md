# loop-zero

A small, versioned agent workflow shared by repositories. This repository owns
the core; product repositories consume identical pinned snapshots and retain
their own commands, architecture, security and release policy.

The portable distribution has two parts pinned by one commit SHA. The vendored
[`core/`](core/CONTRACT.md) snapshot holds the contract, governance skills,
three pinned methodology skills with overlays, one handoff, thin Codex and
Claude entry points, four conditional framework profiles and read-only pin and
check-policy tools. The `loopzero` Python
package, installed from the same SHA, holds the executable mechanisms: the
worktree lease, sandboxed validation children, the signed evidence ledger,
detached jobs, runner adapters, the dispatcher and the delivery state machine,
plus a generator for consumer wiring and Claude/Codex/Cursor skill layouts. The core ships no scheduler service,
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
credential boundary remain active. Neither the outer validation bubble nor the
per-launch wrapper uses `--unshare-net`: version/readiness probes and provider
scenarios require outbound network, while filesystem visibility remains the
positive allowlist described above.

The per-launch wrapper mounts a private tmpfs over `/run`, so it also binds
the resolved target of `/etc/resolv.conf` read-only when that symlink points
into `/run` (systemd-resolved hosts); without that bind every provider request
fails name resolution and both CLIs retry until the scenario timeout.  Codex
`0.154.0` has no `debug.config_lockfile` export, so the SDK bridge applies the
restrictive runtime overrides directly, then uses app-server `config/read`
after initialization and before account or thread use.  It refuses the turn as
unavailable unless every top-level and nested effective key matches the pinned
allowlist: request-specific pins, empty capability registries, and reviewed
native defaults. Unknown keys and changed values fail closed, including shell
environment settings, notification commands, endpoints, providers, and features.
The bridge reads raw configuration dictionaries so SDK decoding cannot discard
unknown nested keys. TUI configuration must be null; local history must retain
its recorded native defaults.  The private `CODEX_HOME` remains empty apart
from its sealed credential and holds no project trust entry, which keeps
project-level `.codex` configuration out of scope.  An enterprise-managed or
cloud-managed behavior not represented by `config/read` remains an explicit
consumer residual requiring acceptance. Every setting returned by `config/read`
is attested; the check does not attest omitted settings such as the pinned
binary's legacy `output_token_limit` override.
The Codex access-only snapshot keeps the source's `last_refresh`
timestamp because `0.154.0` treats a missing timestamp as stale and would
otherwise attempt the refresh the snapshot deliberately cannot perform.  Codex
permission-denial always launches: it passes only with observed denial evidence
from a simple `/etc/shadow` read whose SDK started action and nonzero completion
share an item ID and whose tool output contains the matching complete denial
line. Ambiguous shell syntax, option errors, and denial text without the read
action yield no evidence. An otherwise valid turn without that evidence is
recorded as `unsupported`, with its reason, observed command outcomes, and
charged cost; accepted evidence also records the SDK item ID.  SDK
bridge error frames carry a sanitized `diagnostics` object (exception class and
a redacted, bounded message) that the normalized result surfaces as
`<Vendor> SDK failure: ...`; CLI retry events surface as
`<Vendor> API retry: ...`, and a bridge killed by an external signal before its
terminal frame is a `transport-disconnect` only if its stream is otherwise valid.
Malformed protocol remains a protocol failure even after a signal.

Create a GitHub Actions environment named `nightly-conformance`, restrict its
deployment branches to `main`, and configure **no required reviewers** so the
scheduled job runs unattended. Configure these environment secrets (not
repository-level secrets) with the native subscription-login credential JSON
expected by the corresponding runner broker:

- `LOOPZERO_CONFORMANCE_CLAUDE_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CODEX_CREDENTIAL`
- `LOOPZERO_CONFORMANCE_CURSOR_CREDENTIAL`

A missing secret is a hard job failure. Before checked-out code starts, a
separate host step builds a wheel from the run-pinned commit at `release/0.3`,
installs it into a dedicated venv below `RUNNER_TEMP`, and runs
`loopzero-credential-seal`. The trusted broker validates and, when needed,
refreshes the mode-0600 source, then creates a mode-0600 access-only snapshot
with no refresh token and a clamped expiry. The workflow deletes the source
before starting bubblewrap. Only the snapshot is bound read-only into the
validation bubble, and an access-only snapshot that can no longer cover a
launch becomes cleanly unavailable without any refresh attempt. Neither file
is placed in the checkout or artifacts, and credential contents are never
printed; the seal reports only its bounded source kind (for example,
`oauth-file` or `token-file(default)`). The remaining risk is explicit:
checked-out code on `main` can read
and exfiltrate the short-lived access token while the network-enabled live job
runs. The GitHub environment limits secret scope, but is not a per-run human
approval boundary.

For a host login, the zero-cost readiness preflight uses normal credential
discovery, seals one access-only snapshot outside the scenario wrapper, probes
the exact CLI version and SDK/app-server readiness, and prints only a sanitized
table. Select one runtime and its exact executable:

```bash
LOOPZERO_LIVE_RUNTIMES=codex \
LOOPZERO_LIVE_CLI_PATH=/absolute/path/to/codex \
LOOPZERO_LIVE_CREDENTIAL_SEAL=/absolute/trusted/bin/loopzero-credential-seal \
.venv/bin/python -m pytest -q -s -p no:cacheprovider \
  --basetemp=/tmp/loopzero-live-diagnose --diagnose \
  tests/conformance/live/live_conformance.py
```

The paid live run uses the same once-per-suite host seal and passes only its
access-only result to scenario launches:

```bash
LOOPZERO_LIVE_RUNTIMES=codex \
LOOPZERO_LIVE_CLI_PATH=/absolute/path/to/codex \
LOOPZERO_LIVE_CREDENTIAL_SEAL=/absolute/trusted/bin/loopzero-credential-seal \
.venv/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp=/tmp/loopzero-live-run \
  tests/conformance/live/live_conformance.py
```

Set `LOOPZERO_LIVE_CREDENTIAL_SOURCE` only when intentionally overriding normal
host discovery with a mode-0600 credential file. Claude accepts either its
OAuth credential JSON or a validated `sk-ant-oat01-` token file; the other
runtimes require their native credential JSON. Set
`LOOPZERO_LIVE_CREDENTIAL_SEAL` to select a separately installed trusted
`loopzero-credential-seal` entry point.

The release broker becomes available only after this branch merges into
`release/0.3`: that branch does not contain `credential_seal.py` beforehand,
so the first nightly broker is built from this branch's code after the merge.

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
not a live lookup. A completed turn with zero output tokens, including one
that reports positive input, and incomplete usage remain unknown rather than
priced. Every started invocation that ends without known usage is charged
Claude's native per-run USD cap, Codex's output cap plus a conservative
estimate of one input token per prompt byte, or a fixed USD 0.10 for Cursor,
whose pinned CLI exposes no native cost/token cap. Such invocations count
toward `LOOPZERO_CONFORMANCE_MAX_UNACCOUNTED_RUNS` (default 3). Killed runs count
toward `LOOPZERO_CONFORMANCE_MAX_KILLED_RUNS` (default 3), after which the suite
stops; the aggregate ceiling also stops further launches. Each runtime gets
one private state-root session home bound read-write into both restart/resume
launches and scrubbed at suite end. Offline tests verify the repeated home and
resume argument/SDK option for every vendor; actual provider resume behavior
is verified only by the nightly job's normalized artifact. Cursor success and
restart/resume are recorded as `unsupported` because the pinned CLI cannot
enforce `output_schema`; free-text JSON is never treated as a pass. Each job
uploads only normalized fields and separates known from conservative charges.

Gate A-G2 means two consecutive **scheduled** UTC nights on the default branch
are green for all three runtime jobs. Both nights must have artifacts showing
the exact pins (including Cursor's build hash), every supported scenario
passing, Cursor's schema-dependent success and restart/resume scenarios
explicitly unsupported, and the named **Codex permission-denial exception**
explicitly accepted by the release decision when a launched turn lacks observable
denial evidence and is recorded as unsupported. Both nights must also show
charged aggregate cost within the configured ceiling and known or documented
conservative accounting. A manual dispatch, missing night, skip, cancellation, replay-only run, absent artifact, unknown cost, or failed
runtime cannot substitute for either green night.
