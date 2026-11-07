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
loop-zero ships no collector, schedule or baseline store. IntelFlo is the
worked example: `make health` is its collector and a nightly read-only audit
keeps its reports outside the repository. loop-zero's own collector is
`loopzero check`.

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
For example, the existing [session-audit tools](file:///home/marcin/.cache/lz-logs/sessions-1014/scripts/)
(`audit`, `lz_mine`, `pr_friction`, `compare`) remain consumer-owned, outside core.
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

**Frozen example (IntelFlo, 2026-11-07):** canonical W is
`[2026-10-08T00:00:00Z, 2026-11-07T00:00:00Z)`. Original seed 20261107's
50-PR extended cohort has queue p90 **51.7210 min, n=50**; filtering the same
membership by merge time gives **70.1891 min, n=48**. Excluded short Nov7 rows
are PR 11374 (2.0084 min) and PR 11435 (1.6925 min). Nearest rank
`ceil(0.9*n)` changes from 45 to 44: removing two low values moves original
rank 46 to W rank 44. Residence is first entry→merge including requeues/hold;
this overlapping-cohort difference is no trend and no loop-zero default.
Frozen research evidence: `test-research-20261107/metrics.md` §§1,4,
`p1e-gapfill.md`, and external `/tmp/tqr/scripts/p4a/{measure,validate}.py`
with `/tmp/tqr/cache/p4a/input-manifest.json` (472 inputs).

On the ordinary backlog/code-health pass, route at most three aggregate
off-target repair families to existing consumer work, naming owner and evidence;
keep missing data visible and avoid automatically filing per-metric issues.

**Frozen protection example (IntelFlo, 2026-11-07):** five workflows disabled,
ten jobs suspended, P23=1: no hosted integration/browser protection; local
exact-candidate coverage unknown. The local p1f sample at source `022026af4`
(WSL2, 36 logical CPUs, 62 GiB; disposable PostgreSQL/Redis) ran integration-local
1/2/3: 12,381 cases each, 2/3/1 failures, 13 skipped. Evidence is
`p1f-local-timing.md` and its CSV, not an attestation of a combined candidate.
For a local candidate row, record candidate tree=unknown, landed tree=unknown,
that scope/environment and that evidence: execution exists at the source tree,
but candidate coverage stays unknown until the actual combined tree is attested.
Consumer proof/replacement remains with [IntelFlo Lane P](https://github.com/feder-positronics/intelflo/issues/10948)
and [disabled-heavy coverage work](https://github.com/feder-positronics/intelflo/issues/10842).

**Frozen final-QA budget example:** the proposed complete backend packet is
`fastapi_backend/tests/integration/etl/test_rss_same_item_reingest.py`,
`fastapi_backend/tests/integration/research/test_context_file_collection_roles.py`
and `fastapi_backend/tests/integration/services/test_admission_commit_shield.py`.
Run four complete files under `nextjs-frontend/__tests__/critical/`:
`collection-search-continuity.spec.ts`, `team-membership-transitions.spec.ts`,
`billing-currency.spec.ts`, `inbox-rules-catch-up.spec.ts`. Backend evidence is
116 cases/run, green in three runs, 140.58/142.20/143.02 summed test-seconds;
browser weights total 212.7 s, not measured packet wall time. Actual fit is unproved.

Plan at 64 merges/day; observed REST search was 402/7=57.43/day. Estimate setup
and execution at 10 runner-min each on 8/4 vCPU, $0.016/$0.008 per runner-min:
$0.24/packet, $0.30 with 25% contingency. Full batches, no invalidation assumed:

| Batch target | Packets/day | Nominal $/day | Contingency $/day | $/30 days |
| --- | ---: | ---: | ---: | ---: |
| 1 | 64 | 15.36 | 19.20 | 576 |
| 4 | 16 | 3.84 | 4.80 | 144 |
| 8 (selected estimate) | 8 | 1.92 | 2.40 | 72 |
| 16 | 4 | 0.96 | 1.20 | 36 |

Include setup, partial/failed/retried/cancelled packets and paid other work in
actual spend; flag any paid nonfinal test. Reconcile Blacksmith daily UTC summary
once against allocated breakdown plus unattributed remainder; truncated allocation
is unknown, never free. Cached Nov 6 total $4.479 yields **ALERT >$3/day**.
Batch 8 leaves only $0.60/day for other work; batch_size=1 is the current baseline.
At evenly spaced 64/day, batch hold estimates are 78.75 min average/157.5 min
oldest-member wait. Report hold separately and within first-entry-to-merge latency.
Packing, packet runtime and repository-wide budget compliance need the existing
Lane P cost/queue owner's experiment; no spending gate or scheduler is implied.

**Frozen timing/outcome example:** W=[2026-10-08, 2026-11-07) UTC, p1e local
records: non-ok child CPU is 9,117.48/25,819.56 CPU-min=35.31%, not all avoidable
waste (840 fail, 356 cancelled, 42 starved, 12 timeout, 1,752 ok). Preserve each
outcome. Record queued/start/end separately: admission wait=start−queued,
wall=end−start, CPU=user+system (excludes Docker); absent timestamps stay unknown.
For the p1f source row above, queued/start/end and wait are unknown; integration
wall is 1,490.52/1,511.85/1,505.56 s, while summed test time is a different measure.
Keep source SHA, candidate tree and landed tree separate; never infer one from
another. Scope/environment and evidence belong on every stage row.
The browser preflight failed after 22.04 s with zero executed tests; collection
is not execution. Stable Python 3.14.4 `os.memfd_create` platform failures differ
from the two changing integration IDs in p1f; neither proves intrinsic flakiness.
Read generic SSH-pool connection, capability and capacity from consumer config;
use disposable checkouts without production checkout access or secrets. The
[consumer local-test handoff](https://github.com/feder-positronics/intelflo/issues/11373)
owns admission proof; Lane P owns candidate/environment adapters outside core.

Frozen examples come from `test-research-20261107/metrics.md` section 4 drafts
1–3 and its section 1 M14/M16, `scorecard.md`, `p1e-gapfill.md` and
`p1f-local-timing.md` in the consumer's established research cache. These dated
figures are examples, not live health claims or new authoritative state.
