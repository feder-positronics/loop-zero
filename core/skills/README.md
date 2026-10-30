# Skill routing

Use [delegation guidance](../../docs/DELEGATION.md) and the task repository's
`models.toml` for subagents. `plan`, `implement`, `work-issue` and `typesafe-ai`
are discoverable under `.agents/skills`.

For API-key integrations, follow the [credential convention](../CREDENTIALS.md)
and [setup guidance](../../SETUP.md#api-keys-for-local-agents).

| Name | Use when |
| --- | --- |
| `plan` | Bound an unclear change before implementation: one objective, observable acceptance. |
| `implement` | Deliver a bounded task to readiness; merge only when separately requested. |
| `work-issue` | Deliver one live GitHub issue through verified merge, then closure or explicit partial delivery. |
| `write-design-doc` | Create or refine a durable design contract: exploration, ADR, initiative, blueprint or guide. |
| `grill-me` | Stress-test requested decisions or settle structurally coupled material choices. |
| `design-mockup` | Explore UI direction in a standalone, non-production HTML/CSS mockup. |
| `explain` | Explain complex topics in Markdown, HTML or an evidence-backed briefing. |
| `backlog` | Inspect live GitHub issues, PRs, blockers, collisions and next work read-only. |
| `diagnose` | Diagnose failures and repair the correct owner when commissioned. |
| `resolve-findings` | Decide bounded responses to existing PR threads before applying fixes. |
| `code-review` | Review a frozen code diff against acceptance; emit JSON for `loopzero review`. |
| `review` | Assess an artifact, system, architecture, workflow, agent configuration or idea read-only. |
| `security-review` | Review changes to auth, permissions, secrets, external calls, config, file/shell primitives, serialization or dependencies. |
| `write-tests` | Add or improve behavior-focused tests when testing is the task. |
| `refine-code` | Simplify or delete evidenced dead code while preserving supported contracts. |

`loopzero review` builds its own prompt; `code-review` defines its JSON behavior.
Use `diagnose` to establish causes and repair failures when commissioned.

Deprecated aliases: `review-design-doc` → `review`; `refine-design-doc` →
`write-design-doc`; `fix-failing-tests` → `diagnose`; `remove-dead-code` → `refine-code`.

Optional vendor skills live under [integrations/](../../integrations/).
Consumer aliases need no core directories: `tdd` → `write-tests` (test-first);
`grill-with-docs` → `grill-me`, then `write-design-doc`.
