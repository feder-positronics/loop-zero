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
| `write-design-doc` | Create a durable design contract: exploration, ADR, initiative, blueprint or guide. |
| `grill-me` | Stress-test requested decisions or settle structurally coupled material choices. |
| `review-design-doc` | Assess an existing design's material gaps and implementation readiness without editing. |
| `refine-design-doc` | Converge an existing design through review, focused edits and validation. |
| `design-mockup` | Explore UI direction in a standalone, non-production HTML/CSS mockup. |
| `explain` | Explain complex topics in Markdown, HTML or an evidence-backed briefing. |
| `backlog` | Inspect live GitHub issues, PRs, blockers, collisions and next work read-only. |
| `diagnose` | Prove an uncertain failure's cause with a small reproduction and discriminating experiments. |
| `resolve-findings` | Decide bounded responses to existing PR threads before applying fixes. |
| `code-review` | Review a frozen code diff against acceptance; emit JSON for `loopzero review`. |
| `review` | Assess an artifact, system, architecture, workflow, agent configuration or idea read-only. |
| `security-review` | Review changes to auth, permissions, secrets, external calls, config, file/shell primitives, serialization or dependencies. |
| `write-tests` | Add or improve behavior-focused tests when testing is the task. |
| `fix-failing-tests` | Classify observed test failures and repair the smallest correct owner. |
| `refine-code` | Simplify current-task code while preserving its observable contract. |
| `remove-dead-code` | Remove an evidenced dead-code batch through recoverable quarantine and checks. |

`loopzero review` builds its own prompt; `code-review` defines its JSON behavior.
Use `diagnose` for unknown causes, `fix-failing-tests` for understood failures.

Optional vendor skills live under [integrations/](../../integrations/).
Consumer aliases need no core directories: `tdd` → `write-tests` (test-first);
`grill-with-docs` → `grill-me`, then `write-design-doc`.
