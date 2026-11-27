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
loop-zero ships no recurring code-health collector, schedule or baseline store;
`loopzero check` runs the repository's blocking checks. See [IntelFlo's audit runbook](https://github.com/feder-positronics/intelflo/blob/main/docs/guides/dev-workflow/audit-health.md#standing-nightly-audit)
for a consumer example.

### Anti-slop checks and Rule of Two

When `loopzero review` catches the same mechanically detectable defect or
structural-erosion pattern in two independent PRs, promote it into a
deterministic linter rule, type check, complexity ratchet or test in the owning
repository instead of adding prompt prose—respecting the
[findings rule](../core/CONTRACT.md#findings) by encoding the defect class, never
copying raw PR findings into files or backlogs. Prefer one maintained, pinned
tool per job inside `[checks].commands`: for Python, Ruff
(`UP/B/SIM/PIE/PERF/RUF/RET/ERA/ARG/PGH003/PGH004/C901`, `ARG` off in tests,
existing complexity offenders marked `# noqa: C901` with a reason), `tach` for declared module boundaries, and, where the
repository adopts one, a type checker (`ty` or `basedpyright`; loop-zero itself
runs none yet); for TypeScript, `tsc --noEmit` and Oxlint
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

Delivery coverage reports reuse the consumer's existing audit outputs; they are
reporting conventions, not delivery requirements. Show executed coverage and
complete passing coverage separately, each with suite, location, exact tree,
scope, environment and evidence link. Skipped or disabled jobs and zero-test
fixtures prove no execution. loop-zero's own measurement is
[scripts/retro_metrics.py](../scripts/retro_metrics.py) (delivery windows from
GitHub REST and terminal `loopzero check` receipts), which the
[retro skill](../core/skills/retro/SKILL.md) runs. A consumer's runbook owns its
own metrics, targets and samples.

### Replayable partial digest

The consumer coordinator owns its audit reducer, invocation, paths and retention,
documented in the consumer's runbook; loop-zero adds no collector, state store or
delivery gate for it. A digest freezes its input manifest (source versions, byte
counts, hashes, time windows), and an offline replay from those inputs must give
byte-identical JSON and Markdown; the report records both output hashes and the
verdict. On partial collection or quota exhaustion, publish a partial diagnostic
separately and keep the last complete pair unchanged. Missing data stays unknown,
never zero.
