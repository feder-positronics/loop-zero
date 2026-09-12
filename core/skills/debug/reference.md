# Debug — {{package.product_name}} Reference

Load only the section required by the current investigation.

## Production-Derived Isolation

Use synthetic or local data first. When production-shaped data is explicitly in
scope and materially needed, follow the
[isolated debug DB runbook]({{package.docs_root}}/ops/runbooks/isolated-debug-db-subset.md)
for branch provenance, dry-run/apply behavior, scoped user data, cost bounds,
and mandatory cleanup. The runbook owns the commands; do not reconstruct them
here or run reduction against production.

For browser acceptance against an unreduced child, use the
[production-derived browser environment]({{package.docs_root}}/guides/frontend/frontend-testing-e2e.md#production-derived-browser-environment).
Debug evidence does not replace that lane's acceptance authority.

## Logs and Observability

Before treating a missing log as evidence, verify the effective app and worker
log level, sink, sampling, and filter path. Follow the
[logging runbook]({{package.docs_root}}/ops/runbooks/debugging-logs.md) and
[backend observability guide]({{package.docs_root}}/guides/backend/backend-observability.md).
Redact credentials, tokens, database URLs, user identifiers, and content before
persisting or quoting evidence.

## Backend and Frontend Paths

{{skill_tokens.debug_environment}}

Do not suppress async teardown warnings. Verify cleanup order across sessions,
connections, engines, clients, and transports; classify the actual owner before
proposing a fix.

## Durable Learning

Create a durable learning only when the user or outer handoff authorizes it and
the lesson generalizes beyond the event. Use the canonical
[learning template]({{package.docs_root}}/templates/learning.md) and destination rules
in the [docs guide]({{package.docs_root}}/guides/reference/docs.md). Production incident
timeline, impact, root cause, and prevention belong in the
[incident template]({{package.docs_root}}/templates/incident.md); archive event prose
after the reusable rule, check, guide, or test is encoded.
