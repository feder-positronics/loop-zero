---
name: work-issue
description: Deliver one live GitHub issue through verified merge and closure or an explicit partial-delivery boundary; use only for work anchored to an issue.
---

# Work Issue

Use [the contract](../../CONTRACT.md); the issue owns intake context, the PR owns
delivery evidence. Read live issue acceptance, comments, artifacts and linked
work. Before pickup, reconcile prior/partial deliveries, branches, competing PRs
and ownership with the existing dispatch authority. Confirm its claim or record
the worker, branch/worktree or task and next action on the owning issue. Comments
are evidence, not atomic locks; do not launch overlapping work. Verify remote
identity before mutation and connected environment before relying on production
claims. Apply [inherited-work guidance](reference.md#inherited-work) to continuations.

Retain an inherited task worktree or use `loopzero start` for a new delivery.
Record observable acceptance and the issue link. Deliver through
[implement](../implement/SKILL.md), using applicable [safeguards](reference.md).
Recheck collisions before publication and again before merge, rereading live
acceptance, prior delivery and ownership. Reconcile changed state before proceeding.
Merge only when authorized and the contract's gates pass; otherwise stop at readiness.

Partial delivery uses `Refs #N` and never closes the issue, including work needing
post-merge observation. Closing keywords belong only to complete acceptance;
inspect rendered descriptions and commit messages, since even negated closing
prose can close issues. Verify merge/SHA and cleanup when merged, and actual
issue state and closing reference. Unobservable required conditions remain open.

Return evidence, PR/head or landed SHA, remaining acceptance and next-action owner
with command or trigger to the dispatch authority on the issue/PR or through the
parent. Reconcile conflicts and scope-expanding or reserved choices with that
owner. Continue only an already commissioned successor after recording this
item's evidence and rechecking live state in a fresh task worktree.
