---
name: work-issue
description: >-
  Deliver a GitHub issue or ticket end to end, from intake through verified
  merge and closeout. Use for any implementation or repair work anchored to a
  live issue. For full blueprint lifecycle execution use `execute-blueprint`;
  for a read-only backlog status view use `backlog`.
---

# Work Issue

For a new task, use [loop-zero delivery]({{package.delivery_guide}})
under `AGENTS.md`'s Primary Delivery Contract. Retain issue acceptance and, when
applicable, blueprint lifecycle commands. The legacy procedure below applies
only to runs whose original record selects `intelflo-v1`; never migrate an
existing run, reset its history, or apply its phase choreography to a new task.


Take one live issue from intake to verified merge and closeout. The
[delivery orchestration method]({{package.docs_root}}/guides/reference/delivery-orchestration-method.md)
is the ordered authority for collision protection, worktrees, dispatch, phase
evidence, Review Gate, commit, PR, CI, merge, run logging, and cleanup.

## Operational Spine

```bash
# Intake preflight (exit 3 = closed or competing PR; recheck with --exclude-pr <n> before merge):
{{toolchain.scripts_dir}}/util/check_issue_available.sh <issue-num>
{{toolchain.python}} {{toolchain.scripts_dir}}/util/skill_run_log.py --start --skill work-issue --git-branch <branch>
{{toolchain.commands.delivery_status}}   # after the run starts and at each natural boundary
{{toolchain.python}} {{toolchain.scripts_dir}}/util/delivery_pipeline.py fill --authority-repo "${{package.primary_env}}" --limit 1
# Normal terminal tail: run shared-method `ready`, then its emitted pickup command.
```

Recovery only: use the following command solely for an already-admitted
continuation whose live state names this action, as defined in Closeout below:

```bash
{{toolchain.system_python}} -I "${{package.primary_env}}/{{toolchain.scripts_dir}}/util/pr_closeout.py" --pr <n> --issue <n> --skill work-issue --merge
```

## Boundary And Ownership

- Use for implementation or repair explicitly anchored to a GitHub issue or
  ticket. A read-only status request routes to `backlog`; a direct prompt with
  no issue routes to the narrowest leaf skill; a full accepted blueprint
  lifecycle routes to `execute-blueprint`. A bounded issue nested under one
  leaves blueprint transitions to that outer owner.
- As outer owner, `work-issue` owns acceptance, routing, the full-diff Review
  Gate, docs decision, commit/PR, CI/review convergence, merge, issue closure,
  and cleanup. Nested leaves implement and validate only. Delegate to the
  narrowest leaf skill whose catalogue description matches the issue shape,
  using the skill catalogue and its README router.
- The shared method's terminal re-entry capsule and context-rollover boundary
  are mandatory. Review/fix/CI convergence for this issue remains inside one
  logical task; a different issue begins only in a fresh context via
  `{{toolchain.commands.reentry}}`.

## Issue Intake

- Read the issue body, comments, labels, linked PRs, screenshots, logs, and
  referenced artifacts. Apply the [alignment gate]({{package.rules_root}}/align-gate.mdc)
  only to material gaps they do not answer.
- State the outcome, affected surfaces, non-goals, validation evidence, and any
  unsafe unknown. Safely infer narrow gaps; otherwise ask one focused question.
- Run the shared preflight, issue lock,
  [dedicated worktree]({{package.rules_root}}/parallel-agents.mdc),
  [branch workflow]({{package.rules_root}}/branch-workflow.mdc), and logical-run sequence.
  Exit 3, an open delivery PR, a merged PR, or evidence of prior partial
  shipment routes to reconciliation/CI/closeout—never duplicate implementation.
- Run `{{toolchain.commands.delivery_status}}` after the logical run starts and at each natural
  boundary. Record any required trust manifest, partition, PR checkpoint, or
  owner direction through the shared delivery controller before continuing.
- Verify the remote before mutation and the connected environment before any
  user-reported production query.
- For a managed SystemError tracker, define production acceptance through the
  [managed delivery verification contract]({{package.docs_root}}/guides/backend/backend-observability.md#managed-systemerror-delivery-verification).
- Umbrella issues follow the [issue-boundary policy]({{package.rules_root}}/ops.mdc).

## Delivery Contract

### Subagent Dispatch

- Invoke the narrowest route as nested and follow the shared method's mandatory
  delegation checkpoint and worktree verification contract. Keep the deposit
  inside issue acceptance and verify it independently in the issue worktree.
- Validate against [AGENTS.md]({{package.constraints_file}}), regenerate OpenAPI/client
  artifacts when API shape changes, and prove acceptance behavior—not only
  passing checks. A seeded-browser-contract or prerequisite change includes the
  focused fresh-stack evidence defined by the
  [frontend E2E guide]({{package.docs_root}}/guides/frontend/frontend-testing-e2e.md#fresh-stack-focused-acceptance).
- Before freezing the Review Gate, consume every nested implementation
  receipt and its parent-authored acceptance commands as the local-validation
  boundary defined by the shared method. Missing or failing evidence returns
  to implementation/local validation; it does not enter full-scope review.
- Transition the outer run to `local-validation` before the first post-
  implementation gate and to `review` only after those gates pass. Delivery
  review cannot run during implementation. Required trust-boundary manifests
  and partitions follow the
  [Review Gate § Delivery Controller Guardrails]({{package.rules_root}}/review-gate.mdc)
  and the shared method's § Delivery controller.
- Apply the full-scope [Review Gate]({{package.rules_root}}/review-gate.mdc), including
  its step 6d fit-check for dossier-driven slices. Immediately before freezing
  that gate, run the shared method's base/visual preflight against the
  prospective final PR body.
  Then run `design-handoff` when the
  [docs-impact triggers]({{package.docs_root}}/guides/reference/design-handoff-reference.md)
  apply; otherwise record the skip rationale.
- Issue-local reversible decisions proceed under D-18 and appear in the PR.
  Reserved, outward, costly, irreversible, or scope-expanding calls stop for
  owner direction.
- Commit only through `commit-autofix`. The PR body opens with the shared
  method's plain-language `Context and goal` contract: `Context`, `Problem`,
  and `Goal`, plus a glossary only for necessary terms used verbatim in that
  opening. It also includes the issue reference, validation, Review Gate
  result, docs decision, run-ID marker, and any decisions beyond the issue
  body. Use `Refs` for partial umbrella slices and `Closes` only for the final
  delivery.
- Publish only through the shared method's pinned canonical-primary
  `pr_publish.py`; never invoke the delivery branch's copy or reconstruct a
  ready PR with direct `gh pr create`.
- Before publication, detect whether this exact issue owns a lifecycle
  blueprint through its frontmatter. If it does, route the full lifecycle to
  `execute-blueprint` and include completion in the closing PR. A scoped child
  issue whose number differs from the blueprint tracker does not own or block
  that lifecycle. Closeout verifies this exact ownership and never completes a
  blueprint after merge.
- When the issue owns existing Finding Ledger IDs, follow the single-owner
  `implementation` finding-lease contract in
  [`parallel-agents.mdc`]({{package.rules_root}}/parallel-agents.mdc) and the shared
  method's Finding Ledger trailer contract (§ Review Gate & Commit Boundary).
- Pre-handoff external and dispatched-worker waits follow
  [`ops.mdc` § Long-Running Commands]({{package.rules_root}}/ops.mdc); the session
  ceiling follows the shared method's session-ceiling contract
  (§ Logical-Task Boundary and Re-entry).
- After local validation and normalization, use the shared method's terminal
  handoff: commit, push, open the validated immutable draft identity, then run `ready`
  and its one emitted handoff command. End the foreground executor after
  `review-running` or `candidate-ready/waiting-capacity`; do not wait for
  review, CI, or merge. A `repair-required` terminal starts one separately
  admitted, batched repair round with a bounded delta task.

## Closeout

- Run the shared collision recheck before immutable handoff. The bound
  continuation owns canonical publication, final-gate, merge, remote-state
  verification, run-log, branch, worktree, and recovery-state cleanup. Invoke
  direct `pr_closeout.py` only when recovering an already-admitted continuation
  whose live state names that exact action.
- A named acceptance criterion provable only after merge follows the shared
  method's bounded post-merge acceptance exception — its "sole exception to
  terminal" passage (§ Logical-Task Boundary and Re-entry).
- Post-merge finding reconciliation follows the shared method's Finding Ledger
  trailer contract (§ Review Gate & Commit Boundary).
- Resolve review threads after replying with the fix commit. Late post-merge
  findings use a fresh follow-up worktree and PR; reopen the issue only when a
  previously satisfied acceptance criterion regressed.
- Ordinary monitoring after shipped acceptance is not delivery work. A later
  evidence-only review uses the canonical scheduled-review record and creates
  no reminder issue.

## Evidence And Done

Report issue/acceptance context, worktree and run identity, delegated verdicts,
validation, review and docs decisions, commit/PR/final-SHA evidence, merge and
issue state, thread resolution, and local cleanup. Done means acceptance is
satisfied, no duplicate delivery or unresolved authority stop remains, the PR
is merged, and remote plus local lifecycle state is verified closed.
