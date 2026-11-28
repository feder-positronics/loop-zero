---
name: implement
description: Deliver a bounded task or PR slice with behavior proof. Use work-issue for full GitHub issue delivery.
---

# Implement

Read task text, repository guidance and affected interfaces. Follow the
[delivery steps](../../CONTRACT.md#the-six-steps) in the task worktree.
Keep [credentials out of output](../../CREDENTIALS.md#keep-credentials-out-of-output)
and read the full conventions before API-key integration. Follow
[model selection](../../../docs/DELEGATION.md#resolve-the-model-before-delegation),
[briefs](../../../docs/DELEGATION.md#briefs-and-ownership) and the
[unattended blocker route](../../../docs/DELEGATION.md#worker).

Freeze observable task Acceptance before code edits. Record reasons for changes
in Notes before implementing them. After authorized retargeting, sync PR
Acceptance before review. Before touching guards, auth, permissions, sessions,
secrets or trust boundaries, record threats, invariants and approach in Notes.
Before deduplicating tests, list distinct assertions. Retain each and identify
its surviving test in the PR.

Use [write-tests](../write-tests/SKILL.md) for changed tests. Map migration,
authorization, concurrency/locking and policy edge cases to acceptance evidence.
Observe UI and runtime behavior under actual permissions and follow
[visual proof rules](../../VISUALS.md). Keep unobserved acceptance open with
an owner. Record revision, observations, trade-offs and limits in Notes or
human Validation. [Reuse checks](../../CONTRACT.md#check-reuse-and-delivery-priority).

Use [long-running jobs](../../../docs/DELEGATION.md#long-running-jobs) for
commands expected to exceed about two minutes. Follow brief wait/output limits.
Keep receipts, evidence, review artifacts and handoffs on the PR, issue or
named durable scratch. Reserve `/tmp` for command logs, builds and browser runs.
Start new task worktrees from an existing clone. Follow the contract's
[merge and closure cleanup](../../CONTRACT.md#the-six-steps), then the named
host/repo [retention job](../../../docs/DELEGATION.md#leftover-cleanup).
If no job is named or conditions are unverified, preserve leftovers and report
them. For confirmed-abandoned tasks outside retention scope, send the owner
the [manual procedure](../../../SETUP.md#removing-an-abandoned-task-worktree).

Finish planned tests, docs and generated files before primary review. Follow
the [review budget](../../CONTRACT.md#review-budget) on every pushed head.
Keep suggestions in scope. Rebase onto `origin/<base>` (`[repo].base`, default
`main`) before the first draft push and check. Exclude unrelated base updates
from a pending delta. Follow [stacked delivery](../../../docs/DELEGATION.md#stacked-delivery).

Never bypass the sandbox, change check lists or enable `[checks].network` to
pass. Report base failures without workarounds. For a deterministic out-of-scope
defect that needs a workaround, use a small test-only follow-up PR or an
`urgent` issue naming the blocked PR. Commit yourself outside checks, hooks
and scripts. Update task and PR before adding acceptance-required paths. Obtain
scope decisions before expanding proof seams or ownership. Preserve an
adoptable diff on interruption and report remaining acceptance, checks and
findings under the contract.

Keep host and runner changes to ports, env, paths, services or labels compatible with current main,
or wait for the consuming change to land. Prove the affected job on the changed host with one green
run before enabling it for all runners. Before parking a PR, verify it does not wait on yours. Never park both on each other.
Immediately hand failures outside the contract's owner queue route and their last-green comparison
to [diagnose](../diagnose/SKILL.md). A log entry alone does not hand over the failure.
Before requeueing, follow [dequeue diagnosis](../diagnose/SKILL.md#dequeue-and-repeated-failures).
