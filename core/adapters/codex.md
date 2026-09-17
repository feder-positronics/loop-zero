# Codex entry point

Read [the shared contract](../CONTRACT.md) and `workflow.toml` at the consumer
repository root, then open the relevant generated skill and selected profiles.
Existing root/area instructions and native skills retain their local authority.

The consumer links this file from its existing `AGENTS.md`; preserve that file
and its current product skills. `loopzero sync` generates the declared canonical
skill directory and relative mirrors; check the active runtime's loading
behavior rather than assuming discovery.

Before launching any validation child (including hooks), configure an explicit
environment allowlist without lease/nonce or commit-authority aliases and deny
writes to the parent's Git directory and common Git directory. Use a disposable
copy in an isolated environment if this runtime cannot enforce that boundary;
a linked worktree alone is insufficient. Apply the same boundary to descendants.
Only the owning parent may commit inspected changes. The status environment
check does not prove filesystem isolation.

Resume the original run's contract, immutable package artifact and copied
consumer policy before importing workflow code. An adopted v2 task follows the
shared [delivery procedure](../CONTRACT.md#delivery): owned draft, local checks,
authenticated primary and bounded delta dispositions, result projection, mark
ready, exact-source final CI, authorized merge and verified cleanup. Use the
consumer's actual commands; this adapter grants no review or merge authority.

Preserve historical contracts and every tracked or untracked task change on
interruption. Keep one PR evidence summary and recover from authenticated results
and remote facts. Thread resolution is not finding authority, failed projection
is not another review, and missing or incompatible runtime evidence is not a
reason to silently replace the task's selection.
