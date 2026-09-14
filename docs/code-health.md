# Code-health evidence

`loopzero code-health` measures committed Python and TypeScript/TSX source for
ordinary reviews and existing drift audits. It does not assign a quality grade
or change required checks. Install pinned optional analyzers with
`uv sync --extra code-health` in this checkout, or install
`loopzero[code-health]` in the consumer's approved environment.

Consumers already pinned to Ruff 0.5.7 can retain it: install the
`code-health-duplication` extra and pass `--ruff-version 0.5.7`. The default
remains 0.16.7. Version selection belongs to approved caller policy and is
recorded in both policy and tool metadata; baselines from different versions
are not interchangeable. See the [prepared IntelFlo adapter](../examples/intelflo/README.md).

```sh
loopzero --root /path/to/repository code-health snapshot \
  --head HEAD --scope src --scope tests --test-root tests \
  --exclude 'src/generated/*' \
  --output code-structure.json --report code-structure.md

loopzero --root /path/to/repository code-health compare \
  --base BASE_SHA --head HEAD_SHA --scope src --scope tests --test-root tests \
  --exclude 'src/generated/*' \
  --output code-structure.json --report code-structure.md
```

Use `--signals size` for a dependency-free inventory. Size is always collected;
the default additionally requests complexity and duplication. Output directories
must exist. Exit 0 means requested collection completed, including when candidates
were found. Exit 1 and `valid: false` mean incomplete collection. Source/argument
failures also write an invalid artifact. Adapters must check exit status and
payload, including failure to write the destination.

## Source and policy

Refs resolve to immutable commits. The collector reads regular Git blobs and
never imports candidate code. Dirty files, candidate analyzer configuration,
symlinks, and submodules are not analyzed. Unsupported formats and exclusions
remain visible. Files over 5 MB, source exceeding 100 MB, invalid Python, and
non-UTF-8 supported source make collection incomplete.

The caller supplies trusted roots, exclusions, test roots, and thresholds. In PR
automation obtain the command/tool installation from approved base policy,
never the candidate workflow. Both snapshots use the same policy. Run within
existing validation containment: a restricted subprocess environment is not an
OS sandbox. The initial Linux host contract requires Git at `/usr/bin/git`.

Roots are normalized repository-relative paths. Exclusions use `fnmatchcase`
against the full path (`*` can match `/`). Explicitly exclude SDKs, fixtures,
and vendored source. Use `--test-pattern '*.test.tsx'` and other explicit patterns
for colocated tests. Files outside test roots/patterns count as production.
Exclude or separately measure maintenance/experimental scripts. Size-lint
exemptions are not applied to the inventory.

## Interpretation

Schema version 1 records SHAs, policy/hash, required pinned versions, physical
lines, functions, clone pairs/families, exclusions, coverage, and errors. Tool
version mismatch is an error; declared versions alone do not prove execution.

- Function spans include comments, docstrings, and nested definitions. Do not
  sum overlapping spans as LOC. Ruff C901 runs at threshold zero; unavailable
  values stay null and invalidate requested coverage. TS/TSX complexity remains
  explicitly unsupported.
  Ruff uses an explicit `py313` target, recorded in policy. Source parsing also
  depends on the running Python interpreter; newer unsupported syntax remains
  a coverage gap. A UTF-8 BOM is removed before analysis. Parser failures make
  size/function coverage incomplete; successfully parsed files can still supply
  complexity values. Ruff-only parse failures likewise retain other files' values.
- Production and tests are separate complete duplication corpora. jscpd weak
  mode defaults to 15 lines / 100 tokens as trial thresholds. Physical file
  counts differ from detector source counts, because short files may be
  ineligible. Detector statistics remain separate; percentages are not defects.
- Clone ranges are validated against captured files. Invalid pairs remain in
  raw diagnostic evidence, are omitted from families, and invalidate collection.
  `whole_lines_equal` checks displayed whole lines only. An `exact` detector label
  does not establish interchangeable behavior. Partial fragments may remain
  unclassified; classification is only a triage hint.
- Functions match only by unique path and qualified name. Added, moved, renamed,
  and ambiguous functions remain unmatched. Clone leads mean new or changed
  fragment fingerprints, not proven new duplication: boundary changes and moves
  need source review. Comparison summaries contain at most three leads; JSON
  retains the complete inventory, improvements, and unmatched observations.
  Clone leads mark `fragment_seen_in_base`: true for an unchanged file or a
  previously matched fragment at that path, false for an absent base path, and
  null when membership is uncertain. It does not assert move detection or that
  every family member is newly duplicated.

## Audit and PR integration

Consumer health scripts own invocation, output location, and registration in
existing status. Store `code-structure.json` and Markdown alongside other audit
artifacts. Nonzero exit, missing output, `valid: false`, or wrong source SHA
means unavailable/invalid evidence. Candidates never set `threshold_failed`.
Read aggregate status first; route justified hypotheses through
`code-quality-drift` and existing repair/design skills, without a new queue.

PR consumers may attach comparison output as advisory exact-head evidence;
verify both SHAs and policy. Existing Guardian/C901 required checks retain
their commands and verdicts. This release does not ingest Guardian ownership
records: consumers must reconcile existing same-source findings before
presenting additional findings. Do not automatically publish duplicate findings.

`loopzero.code_health.evidence.collector_status` supplies a pure admission check
for the expected source(s), policy, versions, exit status, and completion. The
consumer owns artifact authentication/transport and status writes. The IntelFlo
example demonstrates registration in its existing collector/status structure.

Shared audit references describe this optional artifact. Actual IntelFlo
health/status and PR wiring needs a reviewed loop-zero revision, consumer
pin/sync, and a thin adapter. This change does not update consumer pins or
establish second-consumer portability. Follow `CODE-HEALTH-PLAN.md` calibration
before extending enforcement.
