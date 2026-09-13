# Skill Routing and Rendering

Top-level directories containing `SKILL.md` are loop-zero governance skills.
The `vendor/` subtree contains pinned methodology skills. Consumer product
skills remain consumer-owned. `loopzero sync` renders this file as the
canonical `<skills_dir>/README.md`, renders skills into `[package].skills_dir`,
and records exact managed paths, types, digests, directories, and mirrors in
`.loopzero/skills-manifest.json`.

## Template configuration

Package and toolchain tokens use the `package.<name>` and
`toolchain.<name>` names visible in the source templates. `product_name` and
`commit_identity` have no package defaults: a render that references either
requires the matching nonempty `[package]` key. Paths otherwise retain the
portable defaults implemented by the renderer. Command tokens are the known
keys under `[toolchain.commands]`; unknown keys are rejected.

Consumer-specific governance prose uses the known `[skill_tokens]` keys.
Product-skill dependencies use `[skill_routes]`, mapping a route key such as
`execute_blueprint` to an existing consumer skill directory. An omitted route
renders as “route unavailable in this consumer” and never links to a missing
skill. Substitution is literal and single-pass. Unknown, malformed, nested, or
unresolved tokens are errors; values may not contain managed markers.

### Consumer compatibility profiles

Consumer-specific token values (the exact block a given consumer must set to
reproduce its imported skills byte-for-byte) live outside the rendered
catalogue, in `core/skills/INTELFLO-PROFILE.md` for the first consumer.

## Maintenance Pipelines

The maintenance lane has two audit feeder→consumer pipelines and the
skill-reflection consumer. Author new janitor skills with this shape in mind.

```text
audit-health  ──┬─►  remove-dead-code   (consumes .audit/<today>/*_queue.json)
                ├─►  archive-docs       (consumes candidate manifest)
                └─►  audit findings     (test suite, coverage, guide drift)

code-quality-drift  ──┬─►  decompose-module  (consumes density findings)
                      ├─►  fix-* skills      (per finding, via resolve-findings)
                      ├─►  resolve-findings  (judgmental routing)
                      └─►  contract prune    (Contract lens → contract-prune-runbook queue → work-issue, one id/PR)

skill-reflection rule (in .agents/rules/)  ──►  skill-health
```

The four `disable-model-invocation: true` skills (`audit-health`,
`code-quality-drift`, `archive-docs`, `remove-dead-code`) never fire as a side
effect of ordinary edits. They consume queues only on explicit invocation and
a deliberate cadence. The user and the autonomous `backlog-drain` improvement
loop hold that invocation authority. The loop acts as the user; the invocation
flags stay. This section is the canonical home of that authority grant.

## Vendored precedence

For a rendered vendored skill, upstream `SKILL.md` is the byte-identical base.
The local `OVERLAY.md` is appended in a delimited managed section. On conflict,
the overlay wins over upstream and the [core contract]({{package.core_contract_from_readme}})
wins over both.
