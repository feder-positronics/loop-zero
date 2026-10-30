---
name: security-review
description: Add security review to diffs changing auth, sessions, permissions, secrets, endpoints, external calls, config, file or shell primitives, serialization, or dependencies.
---

# Security Review

Apply [code-review](../code-review/SKILL.md), including its JSON contract and
missing-evidence failure. Do not edit the tree.

Before judging lines, identify actors, assets, crossed trust boundaries, and
attacker-controlled inputs. Verify trust and controls in code; do not assume them.

## Check

- Secrets, tokens, credentials or PII exposed in code, config, logs, errors or
  tests; CORS and cookie changes that broaden exposure.
- Changed endpoints, actions, and jobs: authentication, authorization,
  tenant/user isolation, admin scope, and 403/404 disclosure.
- Trace attacker-controlled data through validation, queries, shell, paths,
  outbound URLs, serialization, storage, and output.
- Reachable injection, SSRF, traversal, unsafe deserialization, mass assignment,
  replay, races, idempotency failures, and cost or denial amplification.
- Side-effecting tasks re-derive ownership; outbound calls have timeouts,
  validation, and safe logging.
- Dependencies and integrations: credentials, permissions, transport, failures,
  reach, publisher, and supply-chain blast radius.

Depth follows attack surface; a vulnerability class without a reachable path
is not a finding.

## Output

Return only code-review's JSON. Secret exposure or a reachable exploit is
`critical`; a missing control on a real boundary is `important`; hardening is
a `suggestion`. Both blocking severities require `request_changes`.

Finish after assessing each changed boundary and plausible attacker-controlled
path. Uncertainty that prevents a verdict is code-review's missing-evidence
`critical` finding; never add fields outside its JSON.

Empty findings are valid when no defects are found.
Never echo secrets, tokens, credentials, or PII in findings.
