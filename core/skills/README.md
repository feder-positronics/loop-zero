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
| `grill-me` | Stress-test a plan and settle consequential choices, asking one question at a time when requester input is needed. |
| `design-mockup` | Explore a UI direction with a standalone HTML/CSS mockup before production implementation. |
| `explain` | Explain a complex topic in Markdown, requested standalone HTML, or an evidence-backed project briefing. |
| `backlog` | Inspect live GitHub issues and PRs read-only for next work, backlog state, delivery retrospectives or digests. Use retro for weekly workflow decisions. |
| `diagnose` | Diagnose failures and, when commissioned, repair the product, oracle or environment that owns them. |
| [`retro`](retro/SKILL.md) | Judge recent workflow changes and choose at most one next change in the weekly retro. |
| `resolve-findings` | Verify existing PR findings, fix commissioned blockers or hand off repairs, and explain each disposition in-thread. |
| `code-review` | Advisory review of a frozen code diff against acceptance criteria, returning JSON findings; `loopzero review` does not load it. |
| `review` | Assess a non-diff artifact, system, architecture, workflow, agent configuration, or idea without changing it. |
| `security-review` | Add security review to diffs changing auth, sessions, permissions, secrets, endpoints, external calls, config, file or shell primitives, serialization, or dependencies. |
| `write-tests` | Add or improve behavior-focused coverage when testing is the task or guide test design during implementation; use diagnose for observed failures. |
| `refine-code` | Simplify a named or current-task diff, or delete evidenced dead code when explicitly commissioned, while preserving supported contracts. |

Recurring code-health audits follow [CODE-HEALTH.md](../../docs/CODE-HEALTH.md).

Deprecated aliases: `review-design-doc` → `review`; `refine-design-doc` →
`write-design-doc`; `fix-failing-tests` → `diagnose`; `remove-dead-code` → `refine-code`.

Optional vendor skills live under [integrations/](../../integrations/).
