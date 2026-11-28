# Skill routing

Entry skills are `plan`, `implement`, `work-issue` and `write-design-doc`; use the
others as needed.

Install these skills once per machine
([setup](../../SETUP.md#connect-your-agent)); repositories keep no copies and add
project detail through `AGENTS.md` and the docs it links.

For delegation, follow [model selection](../../docs/DELEGATION.md#resolve-the-model-before-delegation).
For API-key integrations, follow [credential conventions](../CREDENTIALS.md).

Each skill's `SKILL.md` frontmatter states when to use it: [`plan`](plan/SKILL.md), [`implement`](implement/SKILL.md), [`work-issue`](work-issue/SKILL.md), [`write-design-doc`](write-design-doc/SKILL.md), [`grill-me`](grill-me/SKILL.md), [`design-mockup`](design-mockup/SKILL.md), [`explain`](explain/SKILL.md), [`backlog`](backlog/SKILL.md), [`diagnose`](diagnose/SKILL.md), [`retro`](retro/SKILL.md), [`resolve-findings`](resolve-findings/SKILL.md), [`code-review`](code-review/SKILL.md), [`review`](review/SKILL.md), [`security-review`](security-review/SKILL.md), [`write-tests`](write-tests/SKILL.md), [`refine-code`](refine-code/SKILL.md).

Recurring code-health audits follow [CODE-HEALTH.md](../../docs/CODE-HEALTH.md).

Optional vendor skills live under [integrations/](../../integrations/); to use one, link
`$SRC/integrations/<name>` as the [setup loop](../../SETUP.md#connect-your-agent) links core skills.
