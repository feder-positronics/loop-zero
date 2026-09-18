# Backlog Reference

## Classification

Derive state from live evidence rather than a private tracker:

- **Active:** an issue has a live delivery PR or recent implementation activity.
- **Blocked:** a named unmet dependency, decision, finding, or required check
  prevents its next accepted action.
- **Next:** accepted and sufficiently specified work with no live collision.
- **Needs design:** implementation depends on an unresolved material design
  boundary; small changes following an established pattern do not need one.
- **Unclassified:** an open issue lacks enough evidence to place safely.
- **Orphan PR:** an open PR has no identifiable issue or accepted task context.
- **Missing PR:** work claims to be active but no live delivery PR or branch can
  be reconciled to it.
- **Stale:** active or blocked work has no meaningful update beyond the
  repository's stated threshold; if none exists, report age without inventing a
  threshold.

Treat draft, active, gated, or ready design status according to the repository's
own convention. Reassess complexity after design decisions converge. Complexity
describes required judgment, not a preferred model or executor.

## Evidence Rules

- GitHub issue and PR state is authoritative for remote work.
- A linked PR, closing keyword, shared issue reference, or explicit comment is
  stronger linkage than title similarity.
- Native sub-issues are stronger parent evidence than prose checklists.
- An exact registered worktree or matching branch is collision evidence. A
  merely similar slug is a prompt to inspect, not proof.
- Required CI and open blocking PR threads determine delivery readiness; do not
  reinterpret them from labels alone.
- A reminder without concrete work is not a new backlog item. Keep a dated
  review in the issue or design decision that owns it.

## Output

For each included item report its identifier and link, observed state, why it is
in that section, relevant dependencies or collisions, last meaningful evidence,
and the smallest justified next action. For re-entry, lead with current delivery
and blockers. For delegation candidates, include bounded acceptance and why no
collision or unresolved design choice prevents pickup.
