---
name: skill-health
description: >-
  Reconcile existing repo skills from reflection, usage, correction, policy, and
  current-model evidence while preserving evaluated quality. Use for one-skill
  janitor work or cross-skill convergence inside each skill's accepted scope.
  For new skills or scope-changing redesign use `write-design-doc`.
---

# Skill Health

Keep existing skills distinct, current-model-native, and outcome-reliable. Cut
ceremony only when {{package.product_name}} quality, philosophy, lifecycle, safety, evidence,
and authority behavior remain equal or better.

## Contracts and Authority

- Apply the [skill-writing guideline]({{package.docs_root}}/guides/reference/reference-agent-commands.md)
  and [skill template]({{package.docs_root}}/templates/skill.md). They own instruction
  placement, progressive disclosure, evaluation, and the non-regression law;
  do not maintain a second copy here.
- Consume evidence through the [reflection rule]({{package.rules_root}}/skill-reflection.mdc)
  and the [agent-system metabolism schedule]({{package.docs_root}}/guides/reference/reference-agent-system.md).
  Source logs remain evidence; consumption never authorizes deleting them.
- When this is the outer mutating skill, use a
  [dedicated worktree]({{package.rules_root}}/parallel-agents.mdc). Nested maintenance
  inherits its outer owner and never mutates another task's worktree.
- When direct work will commit, publish, or merge, start or reuse its
  [standalone leaf run]({{package.rules_root}}/branch-workflow.mdc) immediately after
  mutation is accepted and before the first governed dispatch. Nested
  maintenance inherits the outer run and never starts another.
- Janitor authority covers evidence-backed cuts, deduplication, link/trigger
  repairs, and the smallest restoration of a proven missing constraint. A new
  skill, expanded scope, changed workflow meaning, or new enforcement surface
  first requires an accepted design decision, normally through
  [`write-design-doc`](../write-design-doc/SKILL.md).

## Evidence Set

Read the complete target skill, every direct reference, affected siblings, and
known inbound consumers. Use current evidence, as applicable:

- `{{toolchain.commands.reflection_digest}}` and keyed friction/correction records;
- owner verdicts, open/proposed skill-targeted learnings from
  `{{toolchain.commands.learnings_status}}`, and recurring incidents;
- `{{toolchain.commands.check_skills}}`, run/invoke/output trends, and routing outcomes;
- trigger collisions, reference drift, measured context load, and current-model
  ablation or representative-task results.

Digest staleness is a visible diagnostic, not a reason to refresh a catalogue.
Follow the canonical
[change-triggered semantic evaluation]({{package.docs_root}}/guides/reference/reference-agent-system.md#change-triggered-semantic-evaluation)
method. Stop when there is neither semantic consumer impact nor a concrete
learning question; never select work solely to reduce stale counts.

Absence of an observed failure is not proof that an {{package.product_name}}-specific guard is
obsolete. Token, line, or similarity reduction is supporting evidence only,
never the acceptance oracle.

## Triggered Delivery Retrospective

Use this mode to learn across completed `work-issue` and `execute-blueprint`
runs without adding work to PR closeout; closeout's existing run row, phase
events, CI/review evidence, and re-entry capsule are the evidence packet.

Analyze owner corrections and proved avoidable manual recoveries within 24
hours; handle other signals during the normal reconciliation cycle. Analyze at
most three runs selected by those signals, promoted keyed friction, or an
existing structural-loop threshold; a blocked outcome alone is not a trigger.
Stop with `NO_RETRO_SIGNAL` when the apparent cost is within normal variation
and no correctness or safety invariant is implicated.

For each selected run, use only existing run, phase, dispatch, CI, capsule,
session, and friction evidence, labeling unavailable, estimated, and
overlapping attribution. Never infer run-scoped tokens from cumulative session
totals or equate tokens with financial cost. Rank at most two material cost
centers and test at most three incidents, separating observation from
inference, necessary cost from avoidable waste, and agent error from workflow
design; test competing explanations before promotion — one run remains a
hypothesis unless a deterministic contradiction or a correctness/safety
invariant proves the fault. Propose at most three changes, each with an
existing owner/consumer, measurable benefit, risk/effort, maintenance cost,
and the surface it replaces or removes.

Retrospective selection: the daily metabolism check marks `skill-health` DUE
when a proved owner correction or proof-gated documented-guidance workaround
appears after its last invocation; the three-day cadence governs other signals.
The weekly portfolio pass (`{{toolchain.commands.skill_stats}}`, `{{toolchain.commands.phase_stats}}
DAYS=14`, `{{toolchain.commands.reentry_coverage}}`) compares recurring keys and
phase/run distributions across all completed runs and inspects one
deterministic clean control from the previous complete ISO week — the
lexicographically smallest `run_id` whose outcome is `merged` or
`resolved_no_change`, with complete capsule coverage and no other trigger. The
control tests selector bias; it is not a percentage sample or a new standing
queue. A non-green coverage result is a producer-reliability signal, not
evidence that capsules are hard to read.

Return a concise `Keep / Improve / Stop` owner summary. Persist only validated,
non-duplicate friction dispositions or material Decision Records in their
existing streams; create no per-run Markdown, issue, dashboard, eligibility
flag, or retrospective ledger.

## Treatment Cycle

Apply the authoring guide's [preservation mapping]({{package.docs_root}}/guides/reference/reference-agent-commands.md#preserve-quality-while-simplifying)
and [evaluation contract]({{package.docs_root}}/guides/reference/reference-agent-commands.md#evaluation-contract)
to the smallest evidence-backed treatment. Those contracts own instruction
classification, trigger/body comparisons, model baselines, protected invariants,
and convergence; this skill adds the maintenance-specific obligations:

- Re-read the resulting skill, its direct references, affected siblings, and
  semantically affected consumers. Apply the canonical
  [change-triggered semantic evaluation]({{package.docs_root}}/guides/reference/reference-agent-system.md#change-triggered-semantic-evaluation)
  method; do not refresh unrelated consumers merely for digest age.
- Run the relevant skill/config gates and independent review. A multi-skill
  reconciliation also receives [`code-review`](../code-review/SKILL.md) for the
  complete agent-config scope. Resolve all critical/important findings and
  disposition suggestions against evidence, never brevity alone.
- Stay inside janitor authority above; do not create abstractions merely to
  shorten a skill or introduce new workflow meaning.

For a keyed friction signal that this cycle resolves or dismisses, emit the
matching [`agent_event.py`]({{toolchain.scripts_root}}/util/agent_event.py) `friction`
status with the same stable `issue-key`.
Finish a scheduled sweep with `{{toolchain.commands.meters_snapshot}}`; stage the snapshot only
when it changed. Do not erase qualitative source
evidence merely to make the queue look empty.

## Done When

- Every retained, moved, compressed, added, and removed instruction has a
  preservation/evidence disposition and reachable canonical home.
- Trigger and representative-outcome evidence shows equal-or-better quality,
  philosophy adherence, lifecycle, safety, authority, and validation behavior.
- Target skills, references, siblings, and semantically affected consumers are
  coherent; deterministic gates and required independent review are clean.
- The result is smaller or more precise unless measured restoration required
  growth, and no new ceremony, tracker, skill, or workflow meaning was smuggled
  into janitor maintenance.
