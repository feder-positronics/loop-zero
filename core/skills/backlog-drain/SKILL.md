---
name: backlog-drain
description: >-
  Run an unattended improvement campaign by repeatedly refreshing live state,
  selecting one ready work item, and delivering it through `work-issue`. By
  default continue through design triggers, tracked gaps, coherence, quality,
  and discovery when filed issues empty; "issues only" narrows to existing
  issues. For a single named issue use `work-issue` directly.
---

# Backlog Drain

Continuously finish the highest-value ready work without a shadow backlog,
stale selection, overlapping delivery, or excess owner-attention debt.

## Contracts and Authority

- Use [`backlog`](../backlog/SKILL.md) and GitHub as live selection authority.
  Apply the [operations rule]({{package.rules_root}}/ops.mdc) for collisions, issue
  boundaries, scheduled-review/gate surfaces, tracker mutations, and the
  event-driven wait contract (§ Long-Running Commands) for every external or
  dispatched-worker wait. The session ceiling between work items follows the
  shared delivery method's session-ceiling contract (§ Logical-Task Boundary
  and Re-entry).
- Audit supply brake: above the RF-AUDIT-BACKLOG-1 ceiling
  ([model-routing rule]({{package.rules_root}}/model-routing.mdc)), drain existing
  findings — adjudication and delivery stay unblocked; never work around a
  BACKLOG PAUSE by relabeling the launch.
- As the outer mutating orchestrator, enter one
  [dedicated worktree]({{package.rules_root}}/parallel-agents.mdc) before campaign-owned
  repository changes. Each nested `work-issue` delivery owns its separate
  issue worktree; never reuse the campaign worktree for implementation.
- Each selected issue is a separate logical delivery owned end-to-end by
  [`work-issue`](../work-issue/SKILL.md) under the shared
  [delivery method]({{package.docs_root}}/guides/reference/delivery-orchestration-method.md).
  It owns its worktree, delegation under the
  [model-routing rule]({{package.rules_root}}/model-routing.mdc), review, PR, merge,
  closeout, run log, and re-entry capsule.
- Apply [owner doctrine]({{package.docs_root}}/design/owner-doctrine.md): evidence
  precedes cuts ({{skill_tokens.doctrine_d2}}), at most about five owner decisions may wait ({{skill_tokens.doctrine_d14}}), and
  bounded reversible decisions use earned {{skill_tokens.doctrine_d18}} authority. Never implement a
  cut or Reserved choice autonomously.

Invoking the default campaign authorizes delivery and creation of a concrete,
bounded issue produced by the ladder, subject to the contracts above, plus the
standing duties in
[Dossier Lane and Standing Cadences](#dossier-lane-and-standing-cadences). It
does not authorize reminder-only issues, new product frontiers, blueprint
lifecycle execution, irreversible cuts, or exceeding an explicit
count/time/cost/scope limit. “Issues only” authorizes no generated work and no
standing-cadence duties.

## Work-Source Ladder

Use the first source with an eligible item; filed ready work always outranks
generated work:

1. ready GitHub issues with settled acceptance and no blocker/collision;
2. fired `{{toolchain.commands.orient}}` design triggers, resolved through
   [`resolve-findings`](../resolve-findings/SKILL.md);
3. the next tactical item generated from a strategic dossier
   (`{{package.docs_dir}}/design/dossiers/`, unpaused lanes only — pause state defined in
   [Dossier Lane and Standing Cadences](#dossier-lane-and-standing-cadences)):
   the highest-leverage open gap whose `dossier:` claim currently fails, or
   whose `unverifiable:` marker names a manually checkable condition —
   selected inside the dossier's Chosen Direction and never across its
   Non-Goals, which are scope fences;
4. concrete unwired claims/atlas gaps that earn one bounded issue;
5. evidence-backed composition work from a
   [`coherence-audit`](../coherence-audit/SKILL.md) wire-list, never its cut-list;
6. rotated [`audit-health`](../audit-health/SKILL.md) or
   [`code-quality-drift`](../code-quality-drift/SKILL.md) evidence;
7. one bounded coherence discovery refill only when 1–6 are empty and {{skill_tokens.doctrine_d14}} has
   capacity, then restart at 1.

Select by user value, dependency order, readiness, risk, and the smallest
independently shippable slice—not age or ease alone. A source that needs an
unresolved Reserved/product decision is ineligible; record it once without
creating reminder spam and continue only if owner capacity remains.

## Dossier Lane and Standing Cadences

- A rung-3 generated item becomes one bounded issue that names its target
  claim(s) at design time and carries the "Whole-app fit" section per the
  [review-gate fit-check contract]({{package.rules_root}}/review-gate.mdc) (step 6d) —
  the artifact exists at issue creation, before `work-issue` delivery.
- The lane runs unattended (DR-4). Any owner veto of a dossier-generated item
  pauses that module's lane: record a `vetoed` row in the dossier's Verdict
  Log (a non-outcome lane-pause marker, defined in
  `{{package.docs_dir}}/templates/dossier.md`) — that row is the durable pause state a fresh
  campaign checks. The lane resumes only when a later strategic
  `write-design-doc` pass updates or re-affirms the dossier's Chosen Direction
  after the veto row.
- The scheduled `{{package.weekly_issue_workflow}}` workflow owns Weekly Issue
  posting; a campaign never posts a second copy.
- Meta-lane: the campaign schedules the four maintenance feeders/consumers
  (`audit-health`, `code-quality-drift`, `archive-docs`, `remove-dead-code`)
  under the standing grants in the
  [skills README Maintenance Pipelines note]({{package.skills_readme}}#maintenance-pipelines)
  and
  [reference-agent-system § Metabolism Schedule]({{package.docs_root}}/guides/reference/reference-agent-system.md#metabolism-schedule):
  named cadence rows are hard ceilings, and unnamed skills run on
  queue/evidence availability inside that schedule — never on a newly invented
  cadence. Each maintenance run owns its dedicated worktree; the campaign
  worktree is never used for their mutations.

## Item Transaction and Re-entry

1. Start from `{{toolchain.commands.reentry}}`; verify the candidate against live issue/spec,
   lanes, gates, and competing PRs. For generated work, create one actionable
   issue under the issue-boundary policy before delivery.
2. Invoke `work-issue` and let it reach a terminal merged, blocked, reconciled,
   or no-change capsule. Do not duplicate its implementation or closeout.
3. Continue only after no active lane or task-owned worktree remains for that
   item. Begin the next item in a fresh isolated context and run `{{toolchain.commands.reentry}}`
   again. If isolation is unavailable, record the canonical
   `runtime_no_isolation` boundary and stop before accepting new implementation.

An item failure permits another selection only after terminal evidence and
cleanup. Authentication/live-state failure, an unsafe shared-state condition,
or a blocker affecting the candidate set stops the campaign rather than
producing stale or conflicting work.

At campaign entry run
`{{toolchain.python}} {{toolchain.scripts_dir}}/util/agent_event.py invoke --skill backlog-drain` through
[`agent_event.py`]({{toolchain.scripts_root}}/util/agent_event.py). Item run logs and
capsules are the durable delivery record; the campaign does not maintain a
second checklist or tracker.

## Done When

After the final live refresh, report delivered/merged and closed items,
generated issues, blocked/skipped items with reasons, owner-decision capacity,
remaining highest source, validation/CI limits, terminal capsules, and branch/
worktree cleanup. Stop at the allowed ladder/limit, user interruption, owner
budget, systemic blocker, or no eligible source; never claim completion from a
stale ranking.
