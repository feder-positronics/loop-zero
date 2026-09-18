---
name: backlog
description: Produce a live, read-only GitHub issue and pull-request view of active, blocked, next, stale, orphaned, or poorly coordinated work; use for backlog status or re-entry, not implementation.
---

# Backlog

Produce one live, read-only view of GitHub issues and pull requests that another
actor can continue from. Do not invent work state or implement an item.

## Collect

Confirm the repository and authenticated GitHub access. Read the relevant open
issues, linked or closing PRs, labels, relationships, comments, checks, review
threads, timestamps, and repository design artifacts. Use local branches and
registered worktrees only as independent collision evidence, never as a
replacement for GitHub state.

Read [the backlog reference](reference.md) for classification and output shape.
Use the repository's own labels and documented workflow; do not impose a
universal label taxonomy. If live access fails, stop the live-state claim and
label any repository-only result as partial.

## Compose

Select only sections relevant to the question. Preserve issue and PR identifiers,
state, priority when defined, parent or initiative relationships, acceptance or
design gaps, collision signals, age, and evidence behind each recommendation.
Separate observed facts from recommendations.

Before recommending an executable next item, re-read its issue, acceptance,
linked design artifact, branch or worktree claim, and competing PR state. Treat
another live delivery for the same acceptance as a collision to reconcile, not
an invitation to duplicate work.

Ordinary backlog and re-entry views are read-only. If the requester explicitly
asks for issue grooming, re-read live state before mutation, keep each issue to
one bounded outcome, and report every changed issue field or relationship.

Route delivery to `work-issue`, missing durable design to `write-design-doc`, a
draft design needing convergence to `refine-design-doc`, and a narrative brief
to `explain`.

## Exit

Exit with a concise, evidence-linked live view containing the relevant readiness,
collision, gap, and age signals plus the next justified action. Omit empty
sections, label every source limit, and make no unrequested GitHub or repository
mutation.
