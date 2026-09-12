---
name: archive-docs
description: >-
  Archive manifest-approved terminal docs into recoverable historical copies,
  source tombstones, and digest ledger entries. Batch deterministic design
  records; require explicit semantic review for knowledge-bearing docs. For
  discovering stale docs use `audit-health`.
disable-model-invocation: true
---

# Archive Docs

Reduce historical documentation weight without losing decisions, operational
knowledge, discoverability, or recovery.

## Contracts

- Invocation authority (user or the autonomous improvement loop, on its own
  cadence) is defined once in the
  [skill README's Maintenance Pipelines note]({{package.skills_readme}}#maintenance-pipelines).
- This mutation follows the [worktree rule]({{package.rules_root}}/parallel-agents.mdc)
  and changes only selected candidates, historical copies, source tombstones,
  their policy-selected digests, and existing generated indexes.
- Apply the [documentation guide]({{package.docs_root}}/guides/reference/docs.md) and
  machine [lifecycle policy]({{package.docs_root}}/policies/docs-lifecycle.yaml). Load
  the [archive reference](./reference.md) for commands, paths, event-document
  checks, and tombstone shape. Frozen candidate edits follow the lifecycle
  policy's `freeze` contract.
- The generated candidate manifest is eligibility authority, not a suggestion.
  Never archive an unlisted or blocked file, a row without `tombstone_root`, or
  a living doc type. Its `archive_mode` decides whether a row is safe to batch
  or requires manual review. Route stale-doc discovery to
  [`audit-health`](../audit-health/SKILL.md) and active-guide improvement to
  [`write-design-doc`](../write-design-doc/SKILL.md).

## Fragile Sequence

1. Generate a fresh manifest. Empty `blocked_by` is required. Run the batch
   helper for `archive_mode: batch`; it must skip every unreviewed
   `manual_review` row.
2. For a `manual_review` row, resolve every `review_notes` item and preserve
   reusable knowledge in a living surface. Then pass that exact path through
   the helper's explicit reviewed-path option. Stop on drift or unresolved
   knowledge rather than overriding policy.
3. Let the helper preflight the whole selected wave, move each complete source
   under `tombstone_root`, leave frontmatter plus a one-line historical-copy
   link at the original path, add its one-line digest ledger entry, regenerate
   managed indexes, and refresh the candidate manifest. Do not rewrite
   backlinks or plain-text references.
4. Verify each tombstone resolves to the moved source. Do not combine orphan
   cleanup or unrelated documentation edits with archival.
5. Run full docs verification, which includes lifecycle validation. A failure
   stops further archival; repair forward or restore only task-owned edits while
   preserving unrelated work.

## Evidence and Done

Report selected and skipped manifest rows with reasons, moved paths, tombstone
targets, event-knowledge disposition, validation commands and exit status, and
the regenerated index and manifest results. Done requires every historical copy
to remain recoverable through its source tombstone, all docs gates green, and no
unique active knowledge left only in archived prose. Record a successful
terminal run with the invocation command in the archive reference.
