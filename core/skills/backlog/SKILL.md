---
name: backlog
description: >-
  Produce a live, read-only GitHub issue and pull-request view of what is
  active, blocked, next, stale, orphaned, or missing design/PR coordination. Use
  for backlog status, re-entry, or delegation-candidate questions. For
  implementing an item use `work-issue`.
---

# Backlog

Turn current GitHub and repository design evidence into a disposable,
continuation-ready view without inventing or mutating work state.

## Contracts

- Apply the [operations rule]({{package.rules_root}}/ops.mdc) for GitHub authority, issue
  boundaries, collisions, scheduled reviews, and gate labels. Load the
  [backlog reference](./reference.md) for labels, derived spec/complexity state,
  time-gated routing, commands, and output evidence.
- Use the [backlog workflow]({{package.docs_root}}/guides/dev-workflow/backlog-workflow.md)
  for issue/PR linkage. Delegation views apply the
  [model-routing rule]({{package.rules_root}}/model-routing.mdc); labels describe work
  complexity, not a hard-coded model name.

## Collection and Output

- Confirm authenticated GitHub access and run the deterministic collector that
  matches the question: `{{toolchain.commands.backlog}}`, `{{toolchain.commands.reentry}}`, or
  `{{toolchain.commands.backlog_delegation}}`. Query GitHub directly only for evidence the
  collector does not expose; keep the same issue/PR and docs-derived authority.
- Before recommending an executable next lane, also run `{{toolchain.commands.worktree_claims}}`.
  GitHub is the remote gate and the exact registered worktree/branch is the
  independent local gate. Treat non-current `OWNED-LIVE`, `PARKED-DIRTY`,
  `ATTENTION`, or `UNKNOWN` evidence as a collision until resolved; an
  `IDLE-CLEAN` matching branch must be re-entered or explicitly removed rather
  than duplicated.
- Select only relevant sections and preserve issue/PR identifiers, state,
  priority, complexity, spec readiness, initiative/parent relationships,
  collision signals, age, and the evidence behind each recommendation.
- Separate facts from recommendations. Validate a proposed delegation slice
  against the live issue, linked design artifact, acceptance criteria, and PR
  collision before treating it as executable.

GitHub remains the live source of truth. If authentication or collection fails,
stop the live-state claim; do not substitute hand-maintained Markdown or a
previous report.

## Authority and Routes

- Ordinary backlog and re-entry views are read-only. Groom labels, issue
  boundaries, sub-issues, or PR links only when the user explicitly requests
  mutation, after re-reading live state and applying the reference contracts.
- A reminder-only or evidence-timed review routes to the canonical scheduled
  Decision Record, not a new GitHub issue. Blueprint gates and bounded draft-PR
  observation windows use their distinct lifecycle surfaces from the reference.
- Route issue delivery to [`work-issue`](../work-issue/SKILL.md), missing design
  to [`write-design-doc`](../write-design-doc/SKILL.md), draft design to
  [`refine-design-doc`](../refine-design-doc/SKILL.md), and a narrative re-entry
  brief to [`explain`](../explain/SKILL.md).

## Done When

Return a live, evidence-linked, concise view another agent can continue from,
including relevant readiness/collision/gap signals and the next justified
action. Omit empty or irrelevant sections and label any source limit; make no
unrequested GitHub or repository mutation.
