---
name: security-review
description: >-
  Review pending security-sensitive changes through authorization enumeration,
  data-flow tracing, injection/dependency analysis, and threat modeling. Use
  when auth, middleware, endpoint exposure/auth dependencies, integrations,
  config, security-sensitive server actions, or equivalent trust boundaries
  change. For general code quality use `code-review`.
---

# Security Review

Find exploitable boundary failures in the changed scope. This is the
conditional security section of [code-review]'s single governed delivery
chain; it does not repeat ordinary correctness/style review. Apply it only
when the [Review Gate security triggers] match. Standalone advisory use does
not create delivery-section authority.

## Grounding

Read the full diff, entry points, auth/session dependencies, data stores,
external calls, configuration/dependency changes, tests, and governing security
rules in the [security principles]. Establish actors, assets, trust boundaries,
and attacker-controlled
inputs before judging individual lines.

A governed delivery pass follows the shared
[governed delivery-review invocation].

## Required Checks

- [ ] Scan changed code/config for secret exposure, public env leakage,
  dangerous execution/path primitives, CORS/cookie changes, and new dependencies.
- [ ] Enumerate changed endpoints/actions/tasks and prove authentication,
  authorization, user isolation, admin scope, and 404/403 disclosure behavior.
- [ ] Trace sensitive and attacker-controlled data from input through validation,
  queries, logs, external URLs, serialization, storage, and output.
- [ ] Assess injection, SSRF, path traversal, unsafe deserialization, mass
  assignment, secret/PII exposure, replay/race/idempotency, and denial/cost
  amplification where the path makes them plausible.
- [ ] Verify side-effecting tasks re-establish ownership rather than trusting
  caller-supplied context; external calls have validation, timeouts, and safe logs.
- [ ] Review new dependencies/integrations for credential handling, permissions,
  transport, failure behavior, and supply-chain blast radius.
- [ ] Build a concise threat model for the changed boundary and verify mitigations
  with code/tests or focused checks.

The reviewer chooses depth by attack surface; generic enumeration without a
credible path is not a finding.

## Output

In the chain's `security` section, report completion, verdict, evidence-backed
`critical / important / suggestion` findings using the [canonical finding
schema], a non-empty threat-model summary, and review limits. Route
fixes to the owning implementation skill and confirm them through the
[Review Gate] deterministic or single-delta path; do not restart the full
security review. The governed output follows the [signal budget], emits no
recommended follow-ups, and keeps limitations brief.

Emit `agent_event output --skill security-review` with finding counts; run the
bounded [cross-harness pass] only when standalone and explicitly requested with
a nonempty reason.
Persist and deposit local anchored findings through `record-findings`;
governed dispatches deposit automatically. Never include secrets, tokens,
credentials, or PII in the report.

[code-review]: ../code-review/SKILL.md
[Review Gate]: {{package.rules_root}}/review-gate.mdc
[Review Gate security triggers]: {{package.rules_root}}/review-gate.mdc#security-trigger-paths
[security principles]: {{package.rules_root}}/security.mdc
[canonical finding schema]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md
[signal budget]: {{package.docs_root}}/guides/reference/reference-review-findings-format.md#signal-budget
[governed delivery-review invocation]: {{package.docs_root}}/guides/reference/delivery-orchestration-method.md#governed-delivery-review-invocation
[cross-harness pass]: {{package.rules_root}}/cross-harness-review.mdc

## Exit Criteria

- [ ] Changed endpoints and trust boundaries have auth/ownership proof.
- [ ] Sensitive data flows and plausible attack paths are accounted for.
- [ ] Every accepted critical/important security finding has bounded fix evidence.
- [ ] Residual risk, untested assumptions, and review limits are explicit.
- [ ] Local anchored findings were deposited through `record-findings`.
