# IntelFlo skill classification

The classification is based on each skill's behavioral owner, not on incidental
IntelFlo paths that become renderer substitutions. Counts: **25 governance**,
**9 product**, and **1 audit-campaign**.

| Skill | Class | One-line evidence |
|---|---|---|
| `align` | governance | Resolves material intake ambiguity and authority before execution in any repository. |
| `archive-docs` | governance | Applies a general documentation lifecycle of manifest selection, recoverable archive, tombstone, and verification. |
| `audit-health` | governance | Turns deterministic repository, test, documentation, and gate collectors into routed maintenance work. |
| `audit-surface` | audit-campaign | Its broad/sweep modes own manifest-reserved, fix-first UI campaign units and the campaign checkpoint/resume boundary that the design keeps consumer-side. |
| `backlog` | governance | Provides a read-only issue/PR intake and prioritization view independent of product behavior. |
| `backlog-drain` | governance | Repeatedly selects and delivers ready work while enforcing collision, evidence, and stopping policy. |
| `code-quality-drift` | governance | Audits code against repository rules and stable module boundaries; stack paths are configurable evidence inputs. |
| `code-review` | governance | Reviews diffs for correctness, regression, security triggers, and repository-contract compliance. The intelflo edition overlaps the vendored methodology `code-review`; the consumer copy is preserved until a later consolidation decision. |
| `coherence-audit` | governance | Applies a reusable whole-system coherence, completeness, composition, and value-to-weight review workflow. |
| `commit-autofix` | governance | Runs the real commit-hook pipeline and limits staging/repair authority, regardless of product. |
| `debug` | governance | Uses reproduction, competing hypotheses, and root-cause proof before mutation. It overlaps loop-zero `diagnose`; both remain. |
| `decompose-module` | governance | Splits responsibility-dense Python modules along stable boundaries without behavior change. |
| `design-handoff` | product | Encodes IntelFlo's shipped-document destinations, scenario/ADR lifecycle, and product-specific handoff artifacts. |
| `design-mockup` | product | Specifies IntelFlo UI visual language, standalone mockup conventions, and design-system choices. |
| `execute-blueprint` | product | Owns IntelFlo blueprint activation/status transitions and its phased delivery lifecycle. |
| `explain` | governance | Produces audience-calibrated explanations and status briefings with evidence and uncertainty handling. |
| `fix-failing-tests` | governance | Diagnoses whether production code, a test, or its environment violates the intended contract. |
| `fix-ui-bug` | product | Traces a concrete IntelFlo UI defect through Next.js components, server actions, browser evidence, and backend shape. |
| `fortify-roadmap` | product | Assesses IntelFlo's existing capabilities and emits product-specific hardening artifacts. |
| `frontier-roadmap` | product | Reassesses IntelFlo's product direction and proposes new product frontiers. |
| `generate-parser-rules` | product | Owns IntelFlo wrong-parse/full-text clusters, cleaning-rule locations, and parser fixtures. |
| `grill-me` | governance | Stress-tests coupled decisions and records their authority/status before implementation. |
| `implement-backend` | product | Targets IntelFlo's FastAPI routes, services, tasks, schemas, persistence, and backend test layout. |
| `implement-frontend` | product | Targets IntelFlo's Next.js/React routes, components, hooks, server actions, and frontend test layout. |
| `refine-code` | governance | Performs a bounded no-behavior-change clarity pass over recently touched code. |
| `refine-design-doc` | governance | Converges an existing design artifact through review, decision resolution, edits, and verification. |
| `remove-dead-code` | governance | Quarantines, proves, and removes evidenced dead code with an explicit rollback boundary. |
| `resolve-findings` | governance | Converts review/audit findings into authorized decisions without implementing them. |
| `review` | governance | Routes and performs review at the requested altitude. The intelflo edition overlaps loop-zero's existing short `review`; the consumer copy is preserved until later consolidation. |
| `review-design-doc` | governance | Evaluates design artifacts for fit, architecture, decisions, and implementation readiness. |
| `security-review` | governance | Reviews changed trust boundaries through authorization, data-flow, injection, dependency, and threat-model lenses. |
| `skill-health` | governance | Maintains skill routing and behavior from usage, correction, policy, and evaluation evidence. |
| `work-issue` | governance | Owns issue intake through verified delivery, merge, closeout, and re-entry. |
| `write-design-doc` | governance | Creates durable explorations, ADRs, initiatives, blueprints, or guides before implementation. It complements loop-zero `plan`; both remain. |
| `write-tests` | governance | Adds behavior-focused tests at the cheapest faithful seam and validates the affected suite. |

The existing loop-zero `implement`, `plan`, `review`, and `diagnose` skills are
not consolidated with the more specialized imported skills in this change.
The imported `review` and `code-review` consumer editions are also not rewritten:
an unmanifested same-name consumer skill wins and remains byte-for-byte untouched.
