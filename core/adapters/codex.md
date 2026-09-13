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

Run deterministic gates before the frozen review handoff. Use one independent PR
review and at most one bounded delta; mechanical repairs only rerun gates. Keep
exact-head evidence in the single PR body block, preserve an adoptable diff on
timeout, and follow the shared PR-scoped findings and capped known-gaps policy.
