# Backlog — {{package.product_name}} Reference

## Required Labels

- state: exactly one of `backlog`, `in-progress`, `blocked`, `done`;
- type: one of `bug`, `feature`, `tech-debt`, `blueprint`, `exploration`;
- priority: one of `p0`, `p1`, `p2`, `p3`;
- area: one of `backend`, `frontend`, `docs`, `infra`, `cross-cutting`;
- complexity: at most one of `complexity:junior`, `complexity:mid`,
  `complexity:senior`; optional qualifiers include `performance` and `security`.

There is no `stale` or `spec:*` label. Collectors derive staleness from
`updatedAt` and spec readiness from linked `{{package.docs_dir}}/design/` frontmatter and
blueprint lifecycle directories. Grouping and workflow-owned labels are not
ordinary grooming targets.

## Complexity Tiers

- `junior`: named precedent, settled acceptance, and mechanical verification;
- `mid`: settled design inside established patterns but non-trivial multi-step
  implementation or integration;
- `senior`: unresolved decisions, novel architecture, security-sensitive or
  non-mechanical quality judgment. Use the hardest unchecked umbrella item.

Actual executor selection follows the canonical model-routing policy. When
uncertain between tiers, retain the higher tier until live evidence settles the
decision; do not demote an umbrella from checklist wording alone.

## Spec Readiness

| Derived spec state | Route |
| --- | --- |
| missing and design expected | `write-design-doc` |
| `draft` exploration/blueprint | `refine-design-doc` |
| `ready` planned blueprint | `{{skill_routes.execute_blueprint.name}}` or `work-issue` by scope |
| missing small established bug/debt | `work-issue` / `{{skill_routes.fix_ui_bug.name}}` when no spec is needed |
| `active` / `gated` | already in lifecycle; report, do not pick up again |

Re-check complexity after design converges because settled decisions may reduce
the required judgment tier.

## Time-Gated Boundaries

- A settled decision awaiting elapsed time or evidence uses the
  [scheduled Decision Record]({{package.docs_root}}/guides/reference/reference-review-findings-format.md#scheduled-review-template).
  Do not create or retain a reminder-only issue; create an issue only for
  concrete work produced by the review.
- A blueprint blocked on an external gate uses its one lifecycle tracker and
  the [gate commands]({{package.docs_root}}/guides/dev-workflow/gate-tracking.md).
- An existing implementation branch awaiting a bounded observation window uses
  a draft PR with the `time-gated` label and `## Time Gate` contract. The label
  does not belong on reminder issues or ordinary backlog work.

## Defaults

The active/blocked stale threshold is seven days. Collector section order is
stable for machine readability, while the skill omits empty or irrelevant
sections in a composed human view.

## Deterministic Views

- `{{toolchain.commands.backlog}}`: Active, Blocked, Next, Needs design, Unclassified, Orphan PRs,
  Missing PR, and Stale.
- `{{toolchain.commands.reentry}}`: initiative-grouped work plus gated-blueprint/draft-PR waits.
- `{{toolchain.commands.backlog_delegation}}`: candidate slices derived from live labels, spec,
  unchecked items, commands, and PR collisions; every candidate remains a
  proposal until verified against the issue and design artifact.

`Unclassified` prevents unlabeled open issues from disappearing. `Next` shows
queue age without duplicating it into `Stale`; `Stale` is limited to work that
claims to be active or blocked. Umbrella progress uses native sub-issues first
and sufficiently structured body checkboxes only as fallback.

For an explicitly authorized grooming mutation, apply the issue-boundary and
sub-issue rules in the [backlog workflow]({{package.docs_root}}/guides/dev-workflow/backlog-workflow.md),
then re-run the collector and report the exact changed state.
