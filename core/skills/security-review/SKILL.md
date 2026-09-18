---
name: security-review
description: Review a diff that touches auth, permissions, secrets, external calls, config or dependencies for exploitable boundary failures.
---

# security-review

Read [the contract](../../CONTRACT.md) and the [review](../review/SKILL.md)
skill; this skill adds a security lens and uses the same output shape. Apply
it when the diff changes authentication, authorization, session handling,
endpoint exposure, secrets or config, outbound calls, file or shell
primitives, serialization, or dependencies.

## Ground

From the diff, name the actors, the assets, the trust boundary crossed and
every attacker-controlled input. Judge lines only after that.

## Check

- Secrets, tokens or PII in code, config, logs, error messages or tests.
- Each changed endpoint, action or job: authentication, authorization,
  tenant/user isolation, admin scope, and what 403 vs 404 discloses.
- Attacker-controlled data traced through validation, queries, shell and
  path primitives, outbound URLs, serialization, storage and output.
- Injection, SSRF, path traversal, unsafe deserialization, mass assignment,
  replay and race conditions, cost or denial amplification, where the code
  path makes them plausible.
- Side-effecting tasks re-derive ownership instead of trusting caller context;
  outbound calls have timeouts, validation and safe logging.
- New dependencies: what they can reach, how they fail, who publishes them.

Depth follows attack surface. A vulnerability class without a reachable path
in this diff is not a finding.

## Output

Return the exact JSON object defined in [review](../review/SKILL.md).
Severity: `critical` for a reachable exploit or secret exposure, `important`
for a missing control on a real boundary, `suggestion` for hardening. Put the
threat model summary (two to four sentences) in the `body` of the first
finding, or in a single `suggestion` finding titled `Threat model` when there
are no defects. Never quote a secret in a finding.
