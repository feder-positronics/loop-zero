---
name: align
description: >-
  Resolve material task ambiguity before execution with minimal owner effort.
  Use when a wrong assumption would change scope, direction, audience, risk, or
  success criteria. Run before execution starts, on questions that doctrine,
  evidence, and earned authority leave open; for structurally coupled decisions
  use `grill-me`.
---

# Align

Turn an ambiguous request into a clear-enough execution contract. The goal is
not agreement theater; it is preventing a materially wrong implementation.

## Decision Policy

Before asking:

1. Inspect the request and repository for the answer.
2. Apply [owner doctrine]({{package.docs_root}}/design/owner-doctrine.md) and
   [users and jobs]({{package.docs_root}}/design/users-and-jobs.md).
3. **Dossier-first (tactical work)**: module-scoped tactical ambiguity
   resolves against the module's strategic dossier
   (`{{package.docs_dir}}/design/dossiers/`) — its direction, non-goals, and gaps-as-claims —
   before any owner contact. Only a strategic gap the dossier, doctrine, and
   repo evidence cannot answer reaches the owner, and it travels as a queued
   Weekly Issue direction item (`{{toolchain.commands.weekly_issue}}`), never as a mid-task
   blocking question: take the safest dossier-consistent assumption, log it,
   and proceed. Doctrine reserves are unchanged and still escalate.
4. Apply D-18: if the choice is reversible and inside earned category
   authority, decide it and state the assumption. When material, pass the
   Assumed decision to the executing or record-producing skill for the
   [canonical Decision Record]({{package.docs_root}}/guides/reference/reference-review-findings-format.md#decision-record-schema).
5. Ask only when doctrine is silent/conflicted and the call is Reserved,
   irreversible, outward, costly, scope-expanding, or outside earned authority.

Classify the task as exactly one of:

- **Clear enough** — execute.
- **Assumption-safe** — state the consequential assumption and execute.
- **Clarification-needed** — ask one choice-based question with a recommended
  default.
- **Discovery-needed** — offer 2–4 materially different directions; recommend
  one. Escalate to `grill-me` when three or more coupled choices remain.

## Constraints

- A question must change the result; curiosity does not earn an interruption.
- Classify unresolved, non-discoverable request-template placeholders as
  **Clarification-needed** when their values would materially change execution;
  stop before executing that request.
- Prefer one decision per turn unless the owner invited a batch.
- Never ask for information discoverable from code, docs, issue state, or
  telemetry.
- After the answer, restate alignment once and execute without reconfirming.

## Done When

The task can be executed without a material hidden assumption, or one concise
owner decision has been requested with a recommended default.

## Related

- `grill-me` — structural decision discovery
- `resolve-findings` — decisions arising from existing findings
- `write-design-doc` — durable design work after alignment
