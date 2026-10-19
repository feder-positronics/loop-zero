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
