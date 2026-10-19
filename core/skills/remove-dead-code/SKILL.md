---
name: remove-dead-code
description: Remove supplied or manually identified dead-code candidates in a recoverable batch; preserve uncertain or dynamically reached code.
disable-model-invocation: true
---

# Remove Dead Code

Remove one evidenced batch, preserving a recoverable state. Follow
[the contract](../../CONTRACT.md) in the outer task's worktree or one created
with `loopzero start`. Record base and current-head SHAs.

Use requester-supplied candidates or a bounded manual census. Search both task
base and current tree for definitions, imports, direct/indirect callers, strings,
registries, reflection, plugin discovery, command/route entrypoints, configuration,
migrations, serialization, fixtures, generated consumers, public exports,
documentation and tests. Lint and coverage are supporting evidence, never sole
proof. Uncommitted local work cannot prove unreachability in the base.

Classify candidates as clear removal, live-code refactor or manual review.
Preserve ambiguous and dynamically reached candidates with the evidence that
prevents removal. Route live cleanup to [refine-code](../refine-code/SKILL.md),
design-dependent removal to [write-design-doc](../write-design-doc/SKILL.md).

The recovery order matters:

1. Choose a small coherent batch of clear candidates and the clean commit
   immediately before removal as its rollback point.
2. Quarantine the batch as a reviewable removal diff, retaining exact prior
   content in Git. Keep refactoring and unrelated cleanup out.
3. Inspect dangling exports, empty modules, manifests, generated indexes,
   examples, dependencies and other build, packaging, deployment or documentation
   effects. Commit the batch and run `loopzero check` on the clean head without
   weakening checks.
4. Keep removal only if every relevant check passes. On failure, restore the
   **whole batch** to its rollback point and classify it for refactor or manual
   review before debugging.

In `.loopzero/task.md` Notes, record each candidate's path/symbol, inspected
base/head, searches and runtime/build boundaries checked, evidence that no
supported consumer reaches it, affected tests/artifacts/docs/configuration,
classification, validation, restoration and uncertainty. Exit with every
candidate validated, restored or preserved with a reason, and hand the bounded
diff to normal delivery. Keep no separate queue or sidecar report.
