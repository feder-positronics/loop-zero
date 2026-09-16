# Documentation map

Choose a starting point by task. Consumer policy remains owned by the consumer;
portable agent instructions remain in [`core/CONTRACT.md`](../core/CONTRACT.md).

| Task | Start here |
| --- | --- |
| Understand the project | [Project overview](../README.md) |
| Adopt or update a pinned consumer | [Setup and revision updates](../SETUP.md) |
| Work on executable mechanisms | [Kernel](../src/loopzero/kernel/README.md) and [runners](../src/loopzero/runners/README.md) |
| Validate source changes | [Deterministic CI](../.github/workflows/checks.yml) and [conformance tests](../tests/conformance/README.md) |
| Review authority changes | [Mandatory authority review policy](design/authority-review-bar.md) |
| Operate live conformance | [Nightly runtime conformance](nightly-runtime-conformance.md) and [credentials and renewal](nightly-conformance.md) |
| Interpret code-health evidence | [Code-health reference](code-health.md) |
| Understand architecture and decisions | [Design index](design/README.md), including contracts and proposed changes |
| Inspect strategic findings | [2026-09-16 review](strategic-review-2026-09-16.md), a dated assessment with implementation follow-up |
| Find remaining work | [Curated gaps](../KNOWN-GAPS.md), [code-health plan](../CODE-HEALTH-PLAN.md), and [proposed blockage prevention](design/2026-09-16-preventing-delivery-blockages.md) |

Current instructions, normative policy, and historical evidence serve different
purposes. Design proposals and dated reviews do not override current contracts.
Version and runtime pins should be checked in their owning source files and
workflows; old release-specific sections describe their named migration.
