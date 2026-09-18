# Skill routing

| Name | When to use | Tier |
| --- | --- | --- |
| `plan` | Bound an unclear requested change as one objective with observable acceptance before implementation. | entry |
| `implement` | Deliver a bounded task through tests, checks, a draft PR, review fixes, and the `loopzero ready` gate; merge only when separately requested. | entry |
| `work-issue` | Deliver one live GitHub issue from duplicate-checked intake through verified merge and either closure or an explicit partial-delivery boundary. | entry |
| `write-design-doc` | Create an exploration, ADR, initiative, blueprint, or guide when implementation needs a durable design contract. | entry |
| `grill-me` | Settle coupled material decisions when the requester asks for stress-testing or consequential choices are structurally coupled even without that request. | method |
| `review-design-doc` | Review an existing design artifact for material design and implementation-readiness gaps without editing it. | method |
| `refine-design-doc` | Converge an existing design artifact through review, focused edits, and validation. | method |
| `design-mockup` | Explore a UI direction as a standalone, non-production HTML/CSS mockup. | method |
| `explain` | Explain a complex topic in Markdown, standalone HTML, or an evidence-backed project briefing. | method |
| `backlog` | Produce a live, read-only view of GitHub issues, PRs, blockers, collisions, and next work. | method |
| `diagnose` | Start with one small reproduction and separate environmental from behavioral failure when the cause may be found directly. | method |
| `resolve-findings` | Decide bounded responses to existing PR review threads before fixes are applied. | method |
| `code-review` | Independently review a frozen code diff against its acceptance criteria and emit JSON findings for `loopzero review`. | method |
| `review` | Assess an artifact, system, architecture, workflow, agent configuration, or idea without implementing changes. | method |
| `security-review` | Add a security-boundary review when a diff touches auth, permissions, secrets, external calls, config, file or shell primitives, serialization, or dependencies. | method |
| `debug` | Escalate to ranked, discriminating experiments when initial reproduction leaves multiple plausible causes. | method |
| `write-tests` | Add or improve behavior-focused tests when testing itself is the task. | method |
| `fix-failing-tests` | Classify observed test failures and repair the smallest correct owner. | method |
| `refine-code` | Simplify the current task's code without changing its observable contract. | method |
| `remove-dead-code` | Explicitly remove an evidenced dead-code batch through recoverable quarantine and checks. | method |

Start with `diagnose` for a newly observed failure; use `debug` when that first
reproduction does not isolate the cause and competing hypotheses need explicit
experiments. `code-review` defines the structured frozen-diff review behavior
and matches the JSON accepted by `loopzero review`; the command builds its own
reviewer prompt. `review` is the broader read-only assessment for non-diff
targets.

## Consumer-Side Aliases

These are thin routing aliases for consumers, not additional skill directories:

- `align` routes to `plan`;
- `tdd` routes to `write-tests` in test-first mode;
- `grill-with-docs` routes to `grill-me`, then `write-design-doc` for the
  durable artifact.
