# Documentation map

Start with the [project overview](../README.md). Use this map to find the
instructions or authority for the task at hand.

| Task | Read |
| --- | --- |
| Adopt or update a pinned snapshot | [Setup and revision updates](../SETUP.md) |
| Understand shared workflow and delivery rules | [Core contract](../core/CONTRACT.md) |
| Validate source changes | [Deterministic CI](../.github/workflows/checks.yml) and [delivery requirements](../core/CONTRACT.md#delivery) |
| Understand package boundaries | [Executable core architecture](design/2026-09-11-executable-core-architecture.md) and [kernel guide](../src/loopzero/kernel/README.md) |
| Change authority mechanisms | [Authority review bar](design/authority-review-bar.md) |
| Run or diagnose live runtime conformance | [Nightly operations](nightly-runtime-conformance.md) |
| Configure credentials and host renewal | [Credential operations](nightly-conformance.md) |
| Measure size, complexity, or duplication | [Code health](code-health.md) |
| Find design rationale and proposals | [Design index](design/README.md); check each document's stated status |
| Find current accepted gaps | [Known gaps](../KNOWN-GAPS.md) |

The root README is the entry point. Setup owns adoption instructions; operations
pages own runtime procedures; design records explain decisions and proposals.
The core contract and applicable repository policy remain authoritative.
Consumer-specific commands, architecture, security, and release decisions stay
in the consumer repository.
