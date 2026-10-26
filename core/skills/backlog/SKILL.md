---
name: backlog
description: Recommend next work or report live GitHub backlog state through a read-only, evidence-backed issue and PR view.
---

# Backlog

Return a live view another actor can continue from. For “what should I pick up
next?”, recommend an evidence-backed next item. Report observed drift and missing
issue links as observations. If no item is justified, name the blocker or evidence gap
and the next action. Keep GitHub and the repository read-only unless mutation is
explicitly requested; recommending pickup does not start it (no branch, worktree
or `loopzero start`). Use `gh api --method GET` for REST reads with fields.

GitHub owns remote state. Confirm repository and authenticated access; inspect
relevant open issues, linked/closing PRs, labels, relationships, comments,
checks, review threads, timestamps and design artifacts. Local branches and
registered worktrees supply independent collision evidence. If live access
fails, label any repository-only result partial and make no live-state claim.

Use repository workflow, labels and design statuses. Classify from evidence:

- **Claimed:** an assignee or claim comment identifies the commissioned worker and
  branch/worktree or task, awaiting its first implementation output.
  Do not recommend duplicate pickup; reconcile stale claims with the dispatch
  authority rather than treating age as permission to reclaim.
- **Active:** implementation is underway, supported by recent meaningful output;
  use Active rather than Claimed once implementation output exists.
- **Awaiting review/hold/observation:** implementation has evidence, but review,
  an owner hold or an external observation owns the next action. Name that action
  and its owner or trigger; an open PR alone does not mean active implementation.
- **Blocked:** a named dependency, decision, finding or check prevents the next action.
- **Next:** accepted, sufficiently specified, no live collision.
- **Needs design:** unresolved material design boundary; established small changes
  need no design artifact.
- **Unclassified:** insufficient evidence.
- **Missing issue link:** no linked issue identified.
- **Missing PR:** claimed active work cannot be reconciled to a live PR or branch.
- **Stale:** exceeds the repository's meaningful-update threshold; otherwise
  report age without inventing a threshold.

Before recommending pickup, re-read the issue, acceptance, linked design,
branch/worktree claim, current dispatch authority and competing PRs. Identify the
smallest unmet acceptance and whether its next action is implementation, review,
repair or a real hold. Another delivery for the same acceptance is a collision to
reconcile. Explicit links, closing keywords, shared references
and comments outweigh title similarity; native sub-issues outweigh prose
parentage. Exact worktree/branch matches establish collisions; similar slugs
only warrant inspection. Use [contract blockers](../../CONTRACT.md#blockers)
for readiness, not labels. Reassess complexity after design settles; it describes
judgment, not executor preference.

Lead with the recommendation for next-work questions, current delivery/blockers
for re-entry. Include relevant links, observed state, defined priority,
parentage, acceptance/design gaps, dependencies, collisions, age, last meaningful
evidence, classification rationale and smallest next action, with unrequested
GitHub writes offered for requester approval. For delegation, include bounded
acceptance and evidence that pickup is clear.

Separate observations from recommendations. A missing issue link alone does
not prove absence of accepted task context. Omit empty sections and state source
limits.

For explicitly requested pickup or dispatch, consult the slice's dispatch
authority and write the claim on the existing owning issue or PR before starting
work: assignee or one comment naming worker, branch/worktree or task, and next
action. This does not make comments atomic locks; one dispatch authority per
slice prevents competing pickups. A next-work recommendation remains read-only.

For requested grooming, re-read live state, keep each issue to one bounded outcome,
and report changed fields/relationships. Reminders without concrete work belong
as dated-review recommendations in their owning issue/design, not new items.

Route delivery to [work-issue](../work-issue/SKILL.md), missing design to
[write-design-doc](../write-design-doc/SKILL.md), design convergence to
[refine-design-doc](../refine-design-doc/SKILL.md), narrative briefs to
[explain](../explain/SKILL.md).
