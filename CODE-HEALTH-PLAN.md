# Code-health implementation plan

Updated: 2026-09-14. Status: initial core collector implemented and validated.
Snapshot/comparison CLI, pinned optional analyzers, focused tests, and shared
audit references are implemented in this worktree. See `docs/code-health.md`.
A tested IntelFlo status/PR adapter is prepared in `examples/intelflo/`, with
explicit support for its existing Ruff 0.5.7 and a jscpd-only dependency extra.
Actual consumer adoption is pending the overlapping package cutover in IntelFlo
PR #4419. Guardian ownership ingestion, PR workflow wiring, longitudinal
calibration, and second-consumer validation remain pending.

Implementation scope: `loopzero code-health snapshot/compare`, committed Git
blob inputs, Python spans and complete parsed-function Ruff values, separate
production/test clone corpora, validated line ranges and overlapping families,
versioned JSON and concise Markdown, and optional analyzer dependencies. The
first consumer must still supply explicit exclusions and approved command policy.
Automatic exemption annotations and Guardian ownership ingestion are not yet
implemented. Clone deltas remain candidate relationships requiring source review.

Implementation review: Fable (`claude-fable-5-1`) reviewed the collector and
tests. Confirmed corrections include preserving valid-file complexity when
another file fails parsing, overriding Ruff's built-in excludes, correct physical
newline handling, explicit UTF-8 output, dedenting classification input, balanced
report highlights, and batched Git reads. Its npm/Node and detector-schema
assumptions did not match the installed PyPI jscpd 5.2.0: real-tool fixtures and
repeat scans verified the supported flags, match kinds, and deterministic
normalized statistics. One bounded Fable delta review then examined the repairs
and consumer seam. Post-delta repairs retain valid-file values on Ruff parse
gaps, normalize UTF-8 BOMs, record an explicit py313 target, identify missing
functions, and annotate clone base membership. The plain `match` fixture already
passed before repair; the BOM and per-file gap fixtures reproduced failures and
passed after repair. These final repairs received focused regression checks,
not an additional paid review round. No formal consumer review/adoption is claimed.

Current evidence: 22 collector/adoption tests pass, including real Ruff and Python/TS
clone scans. Repeat loop-zero snapshots produce identical normalized evidence.
A focused scan of 164 files in the previously studied IntelFlo areas completes
and finds nine candidate families, including the re-extraction services and
report-field maps. These counts are calibration leads, not defect judgments.
A full adapter scan of the same IntelFlo commit also completes using its real
Ruff 0.5.7 and pinned jscpd 5.2.0: 2,959 production files and 2,732 test files,
with no collection errors. The adapter registers in an isolated copy of the
existing health status structure; IntelFlo's checkout and dependencies are
unchanged. This validates the prepared adapter, not deployed consumer wiring.
Wheel/sdist builds and an installed wheel's dependency-free size scan pass.
The full ordinary pytest suite passed inside a read-only source/Git sandbox
with `uv` exposed as in CI before the final bounded-review repairs (existing
environment-dependent skips remain). The owning collector/adoption and CLI
tests were rerun after those repairs, along with the real IntelFlo adapter scan.
The shared status check, lock consistency, focused Ruff check, and diff check
also pass. CI installs the optional analyzers so their real-tool fixtures run
on its Python matrix. Live paid runtime conformance was not part of this static
collector change.

This is the consolidated plan from the article discussion, repository
inspection, two Fable design reviews, the greenfield design discussion, and the
IntelFlo source study. It supersedes the earlier temporary proposals.

## Outcome

Give ordinary reviews and periodic audits reliable evidence about growing
maintenance cost: expanding functions, concentrated complexity, and duplicated
behavior or policy that requires synchronized edits.

Build the reusable capability in **loop-zero first**. Use IntelFlo to calibrate
it and validate consumer integration. Keep consumer policy and existing gate
authority in the consumer.

Success means useful problems are identified earlier and later changes require
less repeated work. Lower metric values alone do not demonstrate success.

## Decisions established by the investigation

| Decision | Evidence and consequence |
| --- | --- |
| Include duplication discovery in the initial implementation scope. | IntelFlo has identical small query helpers in three services, repeated report-field maps, and duplicated upload lifecycle logic with corresponding historical fixes. Complexity checks do not cover all these cases. |
| Provide one collector with snapshot and comparison views. | PR reviews need new deterioration; periodic audits also need longstanding hotspots. Both must use consistent measurements. |
| Integrate with existing audits and repair skills. | `audit-health` already consumes deterministic evidence; `code-quality-drift` already tests architectural hypotheses and routes justified repairs. |
| Reuse established analysis semantics. | IntelFlo already uses Ruff C901. A second complexity definition would create competing results. Use a pinned supported Ruff version, recording compatibility with the consumer's version. |
| Keep existing required gates intact. | Guardian already rejects new or worsening complexity violations. The additional report must not downgrade or duplicate that authority. |
| Keep duplication advisory initially. | Scanner matches include useful repetition and ordinary contracts, imports, accessors, or existing helper usage. Reported source ranges also require validation. |
| Defer a combined sloppiness score. | Clone percentages vary with thresholds and corpus eligibility. The exploratory erosion calculation supplies no demonstrated predictive benefit over concrete locations. |
| Keep identifier normalization and near-miss detection experimental. | Normalization increased matches substantially, including obvious noise. Its incremental value has not been established. |

The greenfield decision replaces the earlier consumer-first extraction plan.
The IntelFlo study replaces the earlier decision to postpone all duplication
work. Earlier proposals remain historical context, not parallel plans.

## Architecture and ownership

```text
Explicit source snapshot(s) + consumer scope + pinned tools
                              |
                     code-health collector
                       /               \
              comparison report     snapshot report
                     |                    |
              existing PR review      audit-health
                                           |
                                    code-quality-drift
                                           |
                                existing repair/design skills
```

Loop-zero owns source-bound collection, measurement definitions, validated
artifacts, report generation, and the shared audit references. Consumer-owned
scripts invoke it using their approved scope and tool policy. Avoid a generic
plugin framework; introduce only the adapters required by the initial tools.

Consumers own source roots, generated/test classification, suppressions,
thresholds, CI scheduling, gate requirements, and authority to implement or
publish repairs. IntelFlo's Guardian remains the authority for its existing
C901 check and quality claim.

The base loop-zero runtime remains dependency-free. Analyzer dependencies are
explicit optional development/collector tooling. Imports of ordinary runtime
modules must not require those tools or install them automatically.

## Initial collector contract

The proposed interface has two operations; final command spelling should fit
the existing CLI during implementation:

- `snapshot`: one explicit commit and source scope; produce an inventory.
- `compare`: explicit base and candidate commits, with identical source policy
  and analyzer versions; produce before/after observations.

### Source identity

Resolve refs once to immutable commit SHAs and read Git objects or safely
materialized immutable snapshots. Neither comparison input may silently become
the current working tree. Do not import or execute analyzed source.

The initial interface certifies committed snapshots only. A future dirty-tree
mode would require an explicit tree identity and separate contract; a HEAD SHA
cannot certify uncommitted contents.

Use approved/base policy for comparison. Candidate changes to scope, exclusions,
tool configuration, or thresholds cannot silently weaken their own report.
Run collection within the existing validation-child containment when integrated
with governed execution, without commit or publication authority.

### Measurements

| Signal | Initial coverage and semantics |
| --- | --- |
| Source growth | Added, removed, and net physical lines; production and tests reported separately. Count an unterminated last line. |
| Function size | Python functions and methods, including async and nested definitions. Span is `end_lineno - lineno + 1`; it includes comments, docstrings, and nested definition text. Do not sum overlapping spans as repository LOC. |
| Function complexity | Ruff C901 semantics. Prefer complete function values using an explicitly configured all-function collection mode. If only thresholded consumer diagnostics are available, identify the measurement as censored; absence is not CC zero. |
| Duplication candidates | Python and TypeScript/TSX first-party source, including executable logic and declarative policy data. Use a pinned detector and preserve its mode and limits. |

Other languages or unsupported metrics must be visibly unmeasured. Do not imply
that Python complexity coverage also covers the frontend.

Reuse the size hook's suitable pure parsing logic. Its filesystem readers,
absolute gate thresholds, and exemption predicate are not the collector
contract. The existing near-hard file inventory does not supply a complete
per-function inventory.

### Scope and exceptions

Separate production code, tests, generated/vendored code, and experimental or
maintenance scripts. Exclude generated SDKs and fixtures from the initial
production duplication scan; disclose every exclusion and scanner limitation.

Size-lint exemptions and evidence-backed audit suppressions remain visible as
annotations in raw observations. Apply proposal-routing suppressions under
the consumer's existing rules. A size exemption does not make future growth
invisible. IntelFlo's isolated C901 metric has no per-function exemption to
inherit; its aggregate QG-1 budget is a distinct policy.

### Duplication validity and classification

Treat detector output as candidate evidence. Validate file identity, range
ordering, bounds, and correspondence to the captured source. Preserve reported
match kind and whether exact identity was independently verified. Never assume
that a reported whole span is interchangeable code merely because its label
says `exact`.

Keep raw invalid or ambiguous output for diagnosis. Omit malformed ranges from
ranked source claims, expose the coverage limit, and mark requested collection
incomplete. Representative fixtures must establish detector behavior before
enabling its consumer integration.

Classify candidates as executable logic, policy data, or other repetition such
as signatures, documentation, imports, accessors, and UI structure. Do not
discard all dictionaries/types: repeated policy tables were a useful signal in
IntelFlo. Classification is evidence for triage, not a severity verdict.

Group related pairs into one clone family or maintenance hypothesis. Keep
overlapping-pair counts distinct from unique affected lines and physical-file
coverage. Changes in detector version, mode, or corpus rules invalidate direct
baseline comparisons unless explicitly reconciled.

Use 15 lines / 100 tokens as an initial calibration trial, not a universal
threshold. Start without identifier/literal normalization or near-miss passes.
jscpd is the evaluated candidate tool; its study results do not by themselves
qualify a particular release for production use.

### Output and failure semantics

Produce one versioned JSON artifact plus a concise human-readable view. Record:

- Source SHA(s), scope/policy identity, tool versions and settings.
- Requested, supported, and completed measurements.
- Physical input-file counts separately from detector format/source counts.
- Function identities, locations, before/after values, clone families, and
  annotations for exclusions or suppressions.
- Errors, partial coverage, and links to existing gate evidence where supplied.

Retain valid observations from a partial run, but never report the requested
collection as complete when a tool, parser, required input, or output validator
failed. A successful measurement containing leads is not a measurement failure.

In IntelFlo, register the collector in the existing
`status.json` target/collector structure. Missing or invalid requested evidence
sets `valid: false`, which makes `make health` fail under its existing status
rules. Do not set `threshold_failed` merely because candidates exist. The
separate advisory PR check remains non-required; collector failure must not
accidentally fail an existing required job.

## Report behavior

### Ordinary PR comparison

Highlight up to three strongest newly introduced or worsened observations,
with full data available in the artifact. Record improvements separately and
collapse unchanged metrics to counts rather than repeat old warnings.

Match functions conservatively by path and qualified name. Ambiguous identities
and moves remain unmatched; do not invent historical values or label every
renamed function as newly complex. More sophisticated move detection is not
required for the initial release.

For duplication comparison, analyze the relevant owning corpus in both trees,
including unchanged counterparts, then identify introduced relationships. A
scan of changed lines alone cannot find a new copy of an unchanged helper.
Reconcile detector fingerprints with source locations and configuration; mark
uncertain matching instead of manufacturing a regression.

Where a same-source required check already owns an observation, reference its
result rather than create another finding. In IntelFlo, Guardian retains its
rejected C901 identities; new report highlights focus on additional growth or
duplication evidence. Do not infer ownership from check names or stale results.

Attach the report to the existing exact-head PR evidence. It introduces no
additional reviewer, review round, merge authority, or automatic repair.

### Periodic audit snapshot

Include longstanding hotspots and clone families. Prioritize investigation
using affected policy/behavior, callers, existing dependency graphs, observed
co-changes, and a named upcoming change. Those are selection aids; actual source
inspection determines whether a proposal is justified.

Related evidence must identify compatible source. Refresh stale graphs using
the existing audit rules before relying on their relationships. A report can
truthfully conclude that repeated or complex code should remain as written.

## Existing procedure integration

| Procedure | Integration |
| --- | --- |
| Health collector scripts | Invoke the shared collector once, own execution/output/status handling, and store its artifact in the current audit directory. Do not re-create shell pipelines in skill prose. |
| `audit-health` | Read status before payload, explain unavailable coverage, and route useful structural leads to `code-quality-drift`. Add one artifact-reference entry when implementation exists. |
| `code-quality-drift` | Test explicit hypotheses with source, callers and tests; retain `confirmed / wrong / partial` outcomes in its existing report and plan. |
| `refine-code` | Handle justified local behavior-preserving clarity work. |
| `decompose-module` | Extract a demonstrated stable responsibility only when it hides meaningful complexity and preserves contracts. |
| `write-design-doc` | Own changes to responsibility ownership, interfaces, or caller contracts. |
| `resolve-findings` / security review | Resolve competing responses and assess trust-boundary changes through the existing routes. |
| Active audit campaign | Include the lens in the owning unit's existing inspection pass. Do not create another selector or directory sweep. |

Diagnostic observations do not automatically become issues, formal Finding
Ledger entries, dead-code quarantine candidates, or durable debt. Link to an
existing owning Guardian/campaign item when applicable. Preserve the core's
single evidence and curated known-gaps rules and its precedence for new tasks.

Update shared audit templates in loop-zero and adopt them through reviewed
consumer pins/sync. Do not hand-edit vendored snapshots or generated mirrors.
No new audit skill is needed, and the ordinary review skill need not change for
the first collector delivery.

## Delivery sequence

Each step should be independently reviewable. Complete the work within the
authorized step; later enforcement, publication, and consumer adoption retain
their normal authority requirements.

1. **Core snapshot and comparison foundation.** Implement the immutable-input
   contract, Python size/complexity adapter, JSON schema, concise reports, and
   focused tests. Run against loop-zero itself. Keep analyzer dependencies
   optional and the base package dependency-free.
2. **Duplication adapter and calibration fixtures.** Add Python and TS/TSX
   candidate collection, source-range validation, family grouping, coverage
   metadata, and useful/noisy representative fixtures. This is part of the
   initial capability, delivered separately to keep review manageable. Recheck
   the verified IntelFlo examples without modifying their source.
3. **Existing audit integration.** Connect snapshot artifacts to the health
   collector/status path and update the shared audit references. Adopt into
   IntelFlo through its pin and consumer adapter. Preserve Guardian's current
   command outputs, fixed scope, symbol identity, thresholds, candidate protocol,
   and required verdict. Replacing its implementation is separate migration work.
4. **Advisory PR integration.** Add size/complexity comparison evidence. Enable
   clone-delta highlights once snapshot collection and baseline matching have
   passed calibration. Use explicit advisory classification and keep whole-tree
   health out of required PR contexts. IntelFlo's current scheduled Repository
   Health job runs docs governance; adding scheduled code collection is explicit
   consumer CI work, not an assumed existing feature.
5. **Calibration and portability.** Review a declared sample of roughly ten
   PRs and the next deliberate drift audit through existing monthly calibration.
   Record useful proposals, dismissed noise, review effort, and repeated-edit
   examples. A second real consumer on the same core revision is needed to
   establish portability; fixtures alone are insufficient. Refine or remove
   noisy rules before expanding them.
6. **Separate iterative workflow experiment.** After collection is useful, run
   the controlled successive-feature evaluation below. Treat it as evaluation
   work, not a prerequisite imposed on ordinary delivery.

## Acceptance checks

- Both inputs are the named immutable sources, even if the worktree is dirty.
- Equal sources with equal tools/policy produce deterministic observations;
  timestamps and other provenance do not affect comparison identity.
- Additions, deletions, async/nested functions, ambiguous names, renames, invalid
  syntax, missing refs, unsupported formats, and tool failures are represented
  honestly. Include source-corpus changes and censored complexity observations.
- Clone fixtures cover exact small helper copies, repeated policy maps,
  generated SDK exclusion, signatures/docstrings, imports/accessors, renamed
  identifiers, overlapping ranges, malformed/reversed ranges, and a new copy of
  an unchanged file. Tests verify useful claims and failure behavior rather
  than merely matching one scanner's raw output.
- Coverage reports do not confuse physical files, embedded formats, eligible
  inputs, clone pairs, and unique lines. Detector configuration changes cannot
  silently reuse an incompatible baseline.
- Size exemptions stay observable; existing mandatory failures keep their
  authority; advisory unavailable results are never reported as clean evidence.
- Health integration uses existing artifact/status ownership and creates no
  second finding queue. No imports, mutations, or privilege changes occur in
  the analyzed source.
- Existing relevant unit, packaging, consumer rendering, and integration checks
  pass. Choose tests appropriate to each implementation step; do not trigger
  paid runtime conformance merely for a static report change.

## Enforcement and future evaluation

Initially collect observations and route justified proposals. Keep aggregate
LOC/complexity/clone statistics descriptive and separate. Do not ship a combined
quality grade, automated mass cleanup, or a percentage-based merge gate.

An observed report improvement is not permission to change existing thresholds.
Any new required check or Guardian policy change is an explicit later decision
under consumer policy. IntelFlo's existing ratchet-tightening procedure requires
at least three stable daily observations on distinct measured source SHAs and
declared headroom. The ten-PR usability sample does not replace that requirement.

For iterative evaluation, define one small consumer task with 3–4 successive
external-behavior requests, preserve the agent's code, and start fresh sessions.
Run cumulative independent acceptance tests after each checkpoint. Pin model,
runtime, sampling settings, task inputs and environment, and charge all relevant
implementation/review calls against a predeclared total USD budget, with a
separate time limit and explicit unknown-cost handling.

Compare the same loop-zero configuration with and without the report to isolate
its contribution. A native-workflow versus loop-zero comparison is a separate
question. Keep failed/timed-out runs. A first three-pair, single-task pilot tests
the evaluation machinery; it cannot establish broad superiority. Primary outcome
is retained correctness; secondary outcomes are later-change effort, repeated
edits, review effort and structural observations.

## Evidence and review provenance

The IntelFlo study used commit
`f4daf10dc4df91fa4e8e59522ecf185ccbfebf27`: 3,085 selected source files, including
1,648 Python files and 9,787 Python functions. It verified:

- Identical `_resolve_requested_content_ids` and `_eligible_tier_b_content_ids`
  ASTs across the claim-relation, evidence-card, and measurement re-extraction
  services; the corresponding CC values were 3 and 2.
- Equal 12-entry field maps in agent-task acceptance and claim-support fields,
  plus 11 equal shared entries in report validation with legitimate extra scope.
- Parallel book/PDF upload lifecycle logic and corresponding changes in
  `07d50dae4`, with additional shared-file changes in `661e61724` and `bb04d574f`.

jscpd 5.2.0 reported 20, 100 and 961 matches at 30/150, 15/100 and 5/50
line/token settings, respectively, with percentages from 0.166% to 2.116%.
Identifier normalization at 15/100 reported 261 matches. These are unadjudicated
detector counts with differing eligibility/denominators, not defect counts or
the article's verbosity score. Source inspection and AST/data checks establish
the named examples; scanner labels alone do not.

The Ruff-based erosion analogue was 31.28% at CC>10 and 15.04% at CC>15. It was
an exploratory calculation using a documented source-line definition, not a
reproduction of SlopCodeBench or evidence of future maintenance cost. The
selected manual examples do not supply unbiased precision/recall estimates.

Two earlier design reviews by `claude-fable-5-1` supported the direction with
corrections incorporated here: immutable inputs, exemption visibility, noise
control, preserved gate authority, truthful health status, and honest scope and
budget claims. The later greenfield decision, duplication-study conclusions,
and this consolidated plan have not received a separate Fable review. Normal
independent review remains required for implementation changes.

References:

- [Shared check and delivery contract](core/CONTRACT.md)
- [Maintenance pipelines](core/skills/README.md#maintenance-pipelines)
- [Health collector contract](core/skills/audit-health/reference.md)
- [Drift evidence and routing](core/skills/code-quality-drift/reference.md)
- [Existing size hook](src/loopzero/hooks/size_lint.py)
- [Article](https://earendil.com/posts/measuring-code-sloppiness/)
- [SlopCodeBench paper](https://arxiv.org/html/2603.24755v1)

The measurements and dispositions needed to understand this plan are summarized
above so implementation does not depend on the lifetime of temporary study
files. Raw study artifacts and earlier review transcripts remain available in
the discussion's linked temporary reports.
