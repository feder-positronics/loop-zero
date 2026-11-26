---
name: backlog
description: Inspect live GitHub issues and PRs read-only for next work, backlog state, delivery retrospectives or digests. Use retro for weekly workflow decisions.
---

# Backlog

Answer the requested live-state question from current GitHub and repository
evidence. Default to read-only: a pickup recommendation does not start work or
create a branch/worktree. Confirm the repository and access, then read GitHub
through REST. Inspect relevant issues, acceptance, comments, designs, linked PRs,
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
hold, owner and next action. Among ready items prefer open PRs, then items
whose issue names an open parent step using [plan's `Unblocks:` format](../plan/SKILL.md). Route
authorized pickup to [work-issue](../work-issue/SKILL.md), design to
[write-design-doc](../write-design-doc/SKILL.md), briefs to [explain](../explain/SKILL.md).

For requested grooming, reread live state and report changed fields or
relationships. Reminders without concrete work belong as dated-review
recommendations on their owning issue/design.

For count requests, state the consumer repository's counting rule and reconcile
every open issue into counted work, standing tracking or unclassified; show held
work and classification gaps explicitly. Use consumer AGENTS.md conventions
rather than inventing labels or another state store.

For delivery retrospectives, follow [CODE-HEALTH](../../../docs/CODE-HEALTH.md#delivery-retrospectives);
for a delivery digest, follow its [replay/partial-output contract](../../../docs/CODE-HEALTH.md#replayable-partial-digest).
