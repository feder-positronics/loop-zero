# Remove Dead Code Reference

## Evidence Census

Search the task base and current tree for definitions, imports, direct and
indirect callers, string references, registries, plugin discovery, command or
route entrypoints, configuration, migrations, fixtures, generated consumers,
documentation, and public exports. A linter or coverage signal is supporting
evidence, never sole proof of deadness.

For each candidate record:

- path and symbol or artifact;
- base and current-head SHAs inspected;
- searches and runtime or build boundaries checked;
- why no supported consumer can reach it;
- affected tests, generated artifacts, docs, packaging, or deployment config;
- classification and remaining uncertainty.

## Quarantine Boundary

A quarantine is one recoverable Git diff containing only candidate removals.
The base or a dedicated pre-removal commit is the rollback point. Keep batches
small enough that a failed check can be attributed without investigating a
mixture of removals and refactors.

Before confirmation, review the diff for dangling exports, empty modules,
package manifests, generated indexes, references in examples, and newly unused
dependencies. Use the checks configured for `loopzero check`; do not weaken
them to make a removal pass.

## Handoff Record

In `.loopzero/task.md` Notes, list confirmed removals, restorations, preserved
candidates, validation, inspected SHAs, and evidence limits. The task file and
eventual PR are the record; do not create a sidecar progress file.
