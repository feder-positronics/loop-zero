---
name: work-issue
description: Deliver one live GitHub issue through verified merge and closure or an explicit partial-delivery boundary; use only for work anchored to an issue.
---

# Work Issue

Use [the contract](../../CONTRACT.md); the issue owns intake context, the PR
owns delivery evidence. Follow [delegation guidance](../../../docs/DELEGATION.md)
when choosing executors or splitting work.

## Intake

1. Read the issue, comments, labels, linked PRs and artifacts, including
   screenshots/logs. State outcome, affected surfaces, non-goals, validation
   and unsafe unknowns. Ask one focused question only for material acceptance
   choices the issue cannot answer.
2. Before editing, check issue state, branches, open/merged PRs and prior
   partial deliveries for this exact issue. Reconcile closed, shipped or
   competing work before implementing. Consult the existing dispatch authority
   for this acceptance slice, if any; confirm its recorded claim or, if absent,
   record the worker, branch/worktree or task, and next action on the owning issue
   before pickup.
   A comment is evidence, not an atomic lock; do not launch overlapping work.
   Verify the remote before mutation and the connected environment before relying
   on reported production state.
   When continuing inherited work, apply the [relevance checkpoint](reference.md#inherited-work)
   before integrating its branch; a conflict-free update alone does not establish acceptance.
3. For a new delivery, run `loopzero start issue-<number>-<slug>`; for an
   inherited loop-zero delivery, retain its task worktree and branch after
   reconciling ownership. Write observable Acceptance and the issue link in task Notes.
   **Partial delivery uses `Refs #N`**, including incidents needing an
   observation window after merge. Closing keywords belong only to deliveries
   completing all issue acceptance. Inspect rendered PR descriptions and commit
   messages before publication and merge: even negated closing prose can close
   an issue.

## Deliver and close out

4. Read applicable [safeguards](reference.md). Deliver accepted scope with
   [implement](../implement/SKILL.md) through review and `loopzero ready --wait`.
   Repeat the duplicate-delivery check before `loopzero pr`.
5. **After readiness exits 0, immediately re-read the issue and recheck branches,
   competing open/merged PRs and prior deliveries before merge.** Reconcile
   collisions or changed issue state. Then, in the same turn, from repository
   root run `(cd <worktree> && loopzero merge --wait)`.
6. Verify acceptance, no blocking threads, merged PR/SHA, and branch/worktree
   cleanup. **Verify GitHub shows the issue closed for a closing delivery,
   with the closing reference; for `Refs #N`, verify it remains open** and
   report remaining acceptance. Never close an umbrella issue for partial work.
   For authorized unfinished work, name one next-action owner and its next
   action or actual external trigger. A consumer waiting for a brief names the
   producer, which owns sending it or reporting its blocker. An owner decision
   names the exact question and recipient. Return acceptance evidence, PR/head or
   landed SHA, remaining acceptance, next-action owner and command or trigger to
   the dispatch authority on the owning issue or PR, directly or through the
   parent. When delegated, message the parent mid-task only for a blocker.
   Continue only to an already commissioned successor in the brief if you can
   write Git and post to GitHub yourself. Before starting it, record the current
   head first with its evidence directly on this item's owning PR or issue, then
   recheck live state and start a fresh task worktree. Otherwise get no preassigned
   follow-ups and return after this one item via your final Report to that
   authority.

Report issue, PR, merge SHA, validation, thread state, verified remote issue/PR
state, and connected environment state when acceptance depends on it.

## Stop for

- Scope exceeding one PR: propose boundaries; continue with the requester's selection.
- Conflict between the issue, current code or another delivery: report before editing.
- Requester-reserved, outward, costly, irreversible or scope-expanding choices;
  issue-local reversible choices may proceed.
- Unobservable required post-merge conditions: report them open; do not claim closure.
