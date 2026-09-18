---
name: security-review
description: Review a frozen security-sensitive diff for exploitable boundary failures; use when auth, permissions, secrets, external calls, config, file or shell primitives, serialization, or dependencies change.
---

# Security Review

Find exploitable boundary failures in one frozen diff. Do not edit it.

Read [the contract](../../CONTRACT.md) and [code-review](../code-review/SKILL.md);
this skill adds a security lens and uses the same output shape. Apply
it when the diff changes authentication, authorization, session handling,
endpoint exposure, secrets or config, outbound calls, file or shell
primitives, serialization, or dependencies.

## Ground

From the diff, name the actors, the assets, the trust boundary crossed and
every attacker-controlled input. Judge lines only after that.

## Check

- Secrets, tokens, credentials or PII in code, config, logs, errors or tests;
  CORS and cookie changes do not broaden exposure.
- Each changed endpoint, action or job: authentication, authorization,
  tenant/user isolation, admin scope, and what 403 vs 404 discloses.
- Attacker-controlled data traced through validation, queries, shell and
  path primitives, outbound URLs, serialization, storage and output.
- Injection, SSRF, path traversal, unsafe deserialization, mass assignment,
  replay, race and idempotency failures, and cost or denial amplification where
  the code path makes them plausible.
- Side-effecting tasks re-derive ownership instead of trusting caller context;
  outbound calls have timeouts, validation and safe logging.
- New dependencies and integrations: credentials, permissions, transport,
  failure behavior, reach, publisher, and supply-chain blast radius.

Depth follows attack surface. A vulnerability class without a reachable path
in this diff is not a finding.

## Output

Return the exact JSON object defined in [code-review](../code-review/SKILL.md).
Severity: `critical` for a reachable exploit or secret exposure, `important`
for a missing control on a real boundary, `suggestion` for hardening. Put the
threat model summary (two to four sentences) in the `body` of the first
finding, or in a single `suggestion` finding titled `Threat model` when there
are no defects. Never include secrets, tokens, credentials, or PII in a finding.

Exit when each changed trust boundary and plausible attacker-controlled path
has been assessed, residual uncertainty is stated, and the structured verdict
is complete. Missing diff or required evidence is a `critical` finding, never
an approval.
