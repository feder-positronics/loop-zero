---
name: diagnose
description: Diagnose failures and, when commissioned, repair the product, oracle or environment that owns them.
---

# Diagnose

Follow [the contract](../../CONTRACT.md). Establish expected and observed behavior,
environment, exact command and output; find a small reliable reproduction.
Distinguish local, staging and production evidence. Check tools, dependencies,
network and sandbox without treating an infrastructure failure as a product bug.
Use documented setup and frozen-lockfile procedures for demonstrably stale
dependencies; do not update shared dependencies merely because tools are missing.

Rank competing hypotheses from evidence. Use discriminating experiments and
predicted observations to establish cause; correlation or symptom disappearance
is insufficient. For intermittent failures, use enough observations to distinguish
improvement from chance. Aggregate available logs before adding instrumentation.

Determine whether product, test oracle or environment owns the defect. When
commissioned to repair, fix the smallest correct owner, rerun the exact reproducer
and affected tests, and follow delivery through [implement](../implement/SKILL.md).
Never weaken assertions, delete coverage or broaden retries/timeouts without
support that the old oracle was wrong. Never change product to mask infrastructure
failure. Diagnosis-only commissions stop before implementation.

For bulk GitHub REST reads on App-covered repositories, run
`audit_token=$("$HOME/.config/loopzero/app-token")` once per batch. Require a
successful, nonempty token and use `GH_HOST=github.com GH_TOKEN="$audit_token" gh api ...`.
Keep the token out of arguments, logs, and the parent environment. Check
`/rate_limit` with that credential. Start with one page and lookups for at most
10 runs. Expand only as needed and stop if response headers show quota exhausted.
An App 404 may mean missing repository access. Use a smaller, bounded sample
if the audit needs the shared user credential.

Use synthetic/local data first; redact secrets, database URLs, personal data and
user content from evidence. Instrumentation needs authority, safe output and
recorded removal. Production edits, remote restarts and data mutations require
explicit authority; production-derived experiments need scope and cleanup or
recovery defined beforehand. Reproduction scripts must not commit, tag or push.

Report reproduction, cause, rejected hypotheses, discriminating evidence, blast
radius, repair, validation and uncertainty. If proof is unavailable, name attempts
and missing evidence or authority. Keep out-of-scope repairs open; report
shared-gate impact and the consumer's owned retry, repair or exclusion route;
apply it only when commissioned and authorized, preserving required proof and
protected checks.

## Dequeue and repeated failures

Before requeueing, reproduce the failures named in the dequeue report at the new head. If they also fail on main, follow the [main-failure route](../../CONTRACT.md#blockers).
When the same check fails twice, before any requeue or rerun, diff the last
green run against the first red one: runner or host, environment (ports, env
vars, services, labels) and code. A host or runner change that is live ahead of
the repository change consuming it is an environment cause; name it as one.
