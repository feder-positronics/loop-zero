---
name: backlog
description: Recommend next work or report live GitHub backlog state through a read-only, evidence-backed issue and PR view.
---

# Backlog

Answer the requested live-state question from current GitHub and repository
evidence. Default to read-only: a pickup recommendation does not start work or
create a branch/worktree. Use REST `gh api --method GET`; confirm repository and
access. Inspect relevant issues, acceptance, comments, designs, linked PRs,
checks and threads, plus local branches/worktrees for collisions.

Separate observations from recommendations. If live access fails, label the
result partial/repository-only and make no live-state claims. Age alone does
not release a claim or establish staleness without a repository threshold.
Explicit links and exact branch/worktree matches outweigh title similarity;
missing issue links alone do not prove absent task context. Use
[contract blockers](../../CONTRACT.md#blockers) for readiness.

For pickup, identify unmet acceptance, ownership, collisions and the smallest
next action. Reconcile stale claims with dispatch authority; do not duplicate
accepted work. If no pickup is justified, name missing evidence or the actual
hold, owner and next action. Route authorized pickup to
[work-issue](../work-issue/SKILL.md), design to
[write-design-doc](../write-design-doc/SKILL.md), briefs to [explain](../explain/SKILL.md).

For requested grooming, reread live state and report changed fields or
relationships. Reminders without concrete work belong as dated-review
recommendations on their owning issue/design.

For count requests, state the consumer repository's counting rule and reconcile
every open issue into counted work, standing tracking or unclassified; show held
work and classification gaps explicitly. Use consumer AGENTS.md conventions
rather than inventing labels or another state store.

For delivery retrospectives, use the [code-health examples](../../../docs/CODE-HEALTH.md#delivery-retrospectives): report actual protection at each tree/stage, budgeted final-QA versus billed work, and wait versus execution. Keep unknowns visible; route missing consumer proof to its existing owner rather than adding a gate.

For a delivery digest, follow the [replay/partial-output contract](../../../docs/CODE-HEALTH.md#replayable-partial-digest); link the external consumer reducer and maintainer, retain all 16 views and route at most three aggregate repair families to existing work.
