# Write Tests Reference

Read this reference when selecting test depth or reviewing test quality.

## Boundary Choice

- Unit tests fit deterministic logic and wiring whose collaborators have stable
  contracts. They should not require a real database or network.
- Integration tests fit queries, transactions, isolation, constraints,
  serialization, adapters, and workflows whose behavior depends on real
  boundaries.
- End-to-end tests fit a small number of critical journeys that cannot be proved
  faithfully below the assembled system.

Follow repository placement, fixture, marker, and naming conventions. Prefer
minimum-valid builders, isolated fixtures, and clear arrange/act/assert flow.
Avoid timing tolerance, shared mutable state, duplicate cases, coverage-only
tests, and assertions that merely restate how a mock was configured.
