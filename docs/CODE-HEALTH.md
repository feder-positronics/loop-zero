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
