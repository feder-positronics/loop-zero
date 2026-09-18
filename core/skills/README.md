# Skill routing

| Name | When to use | Tier |
| --- | --- | --- |
| `plan` | Bound an unclear requested change as one objective with observable acceptance before implementation. | entry |
| `implement` | Deliver a bounded task through tests, checks, a draft PR, review fixes, and the `loopzero ready` gate; merge only when separately requested. | entry |
| `work-issue` | Deliver one live GitHub issue from duplicate-checked intake through verified merge and either closure or an explicit partial-delivery boundary. | entry |
| `write-design-doc` | Create an exploration, ADR, initiative, blueprint, or guide when implementation needs a durable design contract. | entry |
| `grill-me` | Settle coupled material decisions when the requester asks for stress-testing or consequential choices are structurally coupled even without that request. | method |
| `diagnose` | Establish the cause of broken behavior with reproduction and evidence before choosing a repair. | method |
| `resolve-findings` | Decide bounded responses to existing PR review threads before fixes are applied. | method |
| `review` | Independently review a frozen diff against its acceptance criteria and emit structured findings. | method |
| `security-review` | Add a security-boundary review when a diff touches auth, permissions, secrets, external calls, config, file or shell primitives, serialization, or dependencies. | method |
