# Skill routing

Use [delegation guidance](../../docs/DELEGATION.md) and the task repository's
`models.toml` for subagents. Install these skills once per machine
([setup](../../SETUP.md#connect-your-agent)); repositories keep no copies and add
project detail through `AGENTS.md` and the docs it links.

For API-key integrations, follow the [credential convention](../CREDENTIALS.md)
and [setup guidance](../../SETUP.md#api-keys-for-local-agents).

| Name | Use when |
| --- | --- |
| `plan` | Plan outcome-sized tasks with observable acceptance, ownership, paths and checks. File authorized slices from active goals or planned blueprints. |
| `implement` | Deliver a bounded task or PR slice with behavior proof. Use work-issue for full GitHub issue delivery. |
| `work-issue` | Own full delivery of a live GitHub issue through verified merge and closure or an explicit partial handoff. |
| `write-design-doc` | Create or refine a durable design contract: exploration, ADR, initiative, blueprint or guide. |
| `grill-me` | Stress-test requested decisions or settle structurally coupled material choices. |
| `design-mockup` | Explore UI direction in a standalone, non-production HTML/CSS mockup. |
| `explain` | Explain complex topics in Markdown, HTML or an evidence-backed briefing. |
| `backlog` | Inspect live GitHub issues and PRs read-only for next work, backlog state, delivery retrospectives or digests. Use retro for weekly workflow decisions. |
| `diagnose` | Diagnose failures and repair the correct owner when commissioned. |
| [`retro`](retro/SKILL.md) | Judge recent workflow changes and pick at most one next change in the weekly retro. |
| `resolve-findings` | Verify existing PR findings, fix commissioned blockers or hand off repairs, and explain each disposition in-thread. |
| `code-review` | Review a frozen code diff against acceptance; emit JSON for `loopzero review`. |
| `review` | Assess an artifact, system, architecture, workflow, agent configuration or idea read-only. |
| `security-review` | Review changes to auth, permissions, secrets, external calls, config, file/shell primitives, serialization or dependencies. |
| `write-tests` | Add or improve behavior-focused tests when testing is the task or guide test design during implementation. |
| `refine-code` | Simplify or delete evidenced dead code while preserving supported contracts. |

`loopzero review` builds its own prompt; `code-review` defines its JSON behavior.
Use `diagnose` to establish causes and repair failures when commissioned.
Recurring code-health audits follow [CODE-HEALTH.md](../../docs/CODE-HEALTH.md).

Deprecated aliases: `review-design-doc` → `review`; `refine-design-doc` →
`write-design-doc`; `fix-failing-tests` → `diagnose`; `remove-dead-code` → `refine-code`.

Optional vendor skills live under [integrations/](../../integrations/).
Consumer aliases need no core directories: `tdd` → `write-tests` (test-first);
`grill-with-docs` → `grill-me`, then `write-design-doc`.
