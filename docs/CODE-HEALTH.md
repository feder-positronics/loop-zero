# Keeping a repository lean

A convention for repositories that use loop-zero, so unused code, stale files
and drift from the repository's own standards are found early and removed
safely. It changes no command, config or state: it is a way to run and read a
recurring audit.

## For the owner

A **code-health run** looks for unused code, dependencies, scripts and
documentation, broken references and departures from the repository's
standards. It produces a short dated report: the revision checked, which
collectors ran and how they ended, and the evidence for each finding.

**Only new findings need attention.** Each run is compared with the previous
complete run; the report lists what was added and what was resolved. Known
findings stay visible in the baseline and their owning work instead of being
repeated every morning. A growing count in an agreed category is a new finding
to route, not a gate.

**A detector's suggestion is a lead, not a decision.** Nothing is deleted
because a tool flagged it. Removal is ordinary commissioned work with its own
review.

**Cadence is the repository's choice.** Start often (for example daily) and
lengthen the interval after a run of quiet, complete reports; go back to the
short interval after a new confirmed problem, a dependency or framework change,
or a collector failure. A missing or failed run is never a quiet one.

## For agents

- **Collect:** run the repository's collector in an isolated checkout. Record
  the revision, tool versions, scope, duration and each collector's exit
  status. A failed, skipped or stale collector is reported as such, never as
  zero findings.
- **Compare:** identify a finding by rule, path and symbol or target, not by
  line number. Report additions and resolutions against the last comparable
  complete run; removing one old finding does not excuse a different new one.
  Keep the previous baseline when collection fails. A tool, scope or config
  change needs an explicit calibration run, not a silent baseline reset.
- **Classify:** *high* confidence is a reproducible standards violation or an
  inspected non-use with every supported entry point accounted for; *medium* is
  a plausible static candidate awaiting those checks; *low* covers age, churn,
  duplication and missing coverage, which help pick what to review but never
  prove deadness. Only high-confidence findings are escalated.
- **Control noise:** prefer correct entry points and generated-code scope to
  broad ignores. An exception names the exact rule and path or symbol, a reason,
  an owner and a review date, in the repository's existing suppression
  mechanism. Report expired or no-longer-matching exceptions; never renew or
  widen them silently.
- **Route work:** follow [BACKLOG.md](BACKLOG.md): search first, one open issue
  per failure family, distinct security or privacy defects kept distinct. The
  standing audit is an ongoing duty; each finite repair is a problem or goal.
  Without authority to publish, hand the report to the owner.
- **Clean up:** discovery grants no implementation authority. Cleanup uses the
  [refine-code](../core/skills/refine-code/SKILL.md) skill only when
  commissioned, in small batches with a rollback point, and merges only with
  its own authorization ([CONTRACT.md](../core/CONTRACT.md)).

Keep each report to about one page: status and revisions, new and resolved
findings, failed or skipped collectors, links to existing issues, and the next
action with its owner. Add no gate, ledger, scheduler or skill for this.

## Consumer choices

Each repository names its collector command, scope, exclusions, cadence,
baseline location and owner in its own `AGENTS.md` or the docs it links.
loop-zero ships no collector, schedule or baseline store. loop-zero's own
collector is `loopzero check`. See [IntelFlo's audit runbook](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/audit-health.md#standing-nightly-audit)
for a consumer example.

### Anti-slop checks and Rule of Two

When `loopzero review` catches the same mechanically detectable defect or
structural-erosion pattern in two independent PRs, promote it into a
deterministic linter rule, type check, complexity ratchet or test in the owning
repository instead of adding prompt prose—respecting the
[findings rule](../core/CONTRACT.md#findings) by encoding the defect class, never
copying raw PR findings into files or backlogs. Prefer one maintained, pinned
tool per job inside `[checks].commands`: for Python, Ruff
(`UP/B/SIM/PIE/PERF/RUF/RET/ERA/ARG/PGH003/PGH004`, `ARG` off in tests), a
per-function complexity ratchet, `tach` for declared module boundaries, and one
type checker (`ty` or `basedpyright`); for TypeScript, `tsc --noEmit` and Oxlint
with `@cloudflare/oxlint-config` (plus restricted-import rules or
`dependency-cruiser` when a consumer needs graph-wide layer rules). Entry-point
graph checks (`knip` for dead files, exports and dependencies; `deptry` for
Python dependency hygiene) start in the consumer's audit collector and graduate
to `[checks].commands` per finding class once an advisory trial demonstrates
reliable results for that repository's entry points and declarations (for
example promoting missing or unlisted dependencies before unused exports or
packages). Heuristic detectors (`vulture`, duplicate finders such as `jscpd`)
stay in audit collectors, never blocking gates. Pin exact versions, keep checks
runnable offline, and fix findings rather than suppress them (any suppression
carries its reason). When introducing a new check to a consuming repository,
trial it advisory-first on historical and live PRs in one repository to measure
true catches against false alarms before making it blocking.

## Delivery retrospectives

Use existing consumer audit outputs; these are reporting conventions, not delivery
requirements. Freeze the M16 applicable critical-obligation denominator before
outcomes, by stage/tree/policy. Show executed coverage and complete passing
coverage separately, with suite, local/final-cloud location, exact tree, scope,
environment, timestamp and evidence link. Disabled jobs remain in the denominator;
authored-head receipts, skipped jobs and zero-test fixtures prove no candidate
execution. Keep scheduled Canary and Delivery Health signals separate.

### Replayable partial digest

The consumer coordinator owns the existing external audit reducer, invocation,
paths and retention; link its runbook and current maintainer in each report.
For IntelFlo, the [Lane P coordinator](https://github.com/feder-positronics/intelflo/issues/10948)
owns the external session-audit handoff (`audit`, `lz_mine`, `pr_friction`,
`compare`); its consumer runbook must identify the current reducer maintainer.
No loop-zero collector, command, state store or delivery gate is implied.

Freeze an input manifest (source/query versions, byte counts and SHA-256 hashes),
UTC half-open windows, cohort membership/seed, reducer/config version and causal
classifications. Offline replay must never collect: run twice from those inputs
and compare JSON and Markdown bytes, with stable ordering/precision and no live
clock fields. The report records both output hashes and the replay verdict.
Each of M01–M16 retains source/evidence, window, eligible/observed n, numerator and
denominator where applicable, value/unit, target, action owner and null reason.
Unknown targets/owners must be explicit; consumer targets require calibration.

| IDs | Required views |
| --- | --- |
| M01–M04 | Stage feedback; inclusive queue residence; dequeue causes; per-job flake/recovery |
| M05–M08 | Per-test flake; confirmed escapes/detection stage; spend/machine-minutes; equivalent local/CI duplication |
| M09–M12 | Valid receipt reuse; review rounds; finding yield/disposition; red-time/unattended subset |
| M13–M16 | Rerun cost; cancelled/never-started cost; completeness/comparability; critical protection/disabled age |

Require external reducer fixtures with these observable results:
zero denominator → null with reason; duplicate immutable IDs/attempts → one
observation; recovery after window end → censored, never success; overlapping
red episodes → interval union within a versioned series, never summed across
workflows; truncated billing → known summary plus unknown allocation, never zero
or scaled-up estimates. Skipped/cancelled executions are separate from flake
eligibility; their costs remain included. Missing acknowledgement makes
unattended time unknown. Publish a partial/stale diagnostic separately on partial
collection or quota exhaustion; atomically replace the last complete JSON/Markdown
pair only after complete validation, preserving its bytes otherwise. Fixture
proof includes that preservation and two offline byte-identical output pairs.

The [IntelFlo queue cohort](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/code-health.md#queue-cohort)
illustrates how filtering a fixed membership by merge time can change queue
percentiles. Retain the reducer, inputs and manifest at a durable evidence URL
before claiming independent JSON and Markdown replay.

On the ordinary backlog/code-health pass, route at most three aggregate
off-target repair families to existing consumer work, naming owner and evidence;
keep missing data visible and avoid automatically filing per-metric issues.

The [IntelFlo local protection sample](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/local-heavy-checks.md#local-protection-sample-from-2026-11-07)
records source-tree execution. Report its scope and environment and leave the
candidate and landed trees unknown until there is evidence for each.
Consumer proof/replacement remains with [IntelFlo Lane P](https://github.com/feder-positronics/intelflo/issues/10948)
and [disabled-heavy coverage work](https://github.com/feder-positronics/intelflo/issues/10842).

Use measured packet wall time to validate final-QA cost estimates. Include setup,
partial, failed, retried and cancelled packets and other paid work in actual spend.
Flag paid nonfinal tests and reconcile daily UTC spend against allocated costs
plus the unattributed remainder. Incomplete allocation leaves cost unknown.
Report batch hold separately and within first-entry-to-merge latency. See the
[IntelFlo final-QA budget study](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/code-health.md#final-qa-packet-and-queue-cost)
for the packet assumptions and hold estimates.

Keep failures, cancellations, starvation, timeouts and successes separate.
Record queued, start and end timestamps. Admission wait is start minus queued,
wall time is end minus start and CPU time is user plus system time. Missing
timestamps leave the corresponding durations unknown. Report executed tests
separately from collection and record the source SHA, candidate tree, landed
tree, scope, environment and evidence for each stage. See [IntelFlo's timing
and outcome sample](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/code-health.md#local-timing-and-outcomes).

Read generic SSH-pool connection, capability and capacity from consumer config;
use disposable checkouts without production checkout access. An owner-trusted
pool host may hold test-scoped credentials only: never production or deploy
keys, short-lived per job where possible, files mode 600, never in argv or logs.
Dependency setup comes from the checked-out repository (its own preparation
script, otherwise its lock files), not from another repository's tooling. Pool
hosts run jobs under the repository-pinned interpreter (for example a user-level
`uv` with the pinned Python), never the host's system Python, and receive no jobs
while their reported toolchain differs from the pin. Install that interpreter with
umask 022: a group- or world-writable install fails consumers' interpreter checks. See [IntelFlo's local test admission handoff](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/local-heavy-checks.md#local-protection-sample-from-2026-11-07)
for consumer-owned proof and adapters.

Frozen examples come from `test-research-20261107/metrics.md` section 4 drafts
1–3 and its section 1 M14/M16, `scorecard.md`, `p1e-gapfill.md` and
`p1f-local-timing.md` in the consumer's established research cache. These dated
figures are examples, not live health claims or new authoritative state.
