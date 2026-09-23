---
name: typesafe-ai
description: Design or implement TypeSafe judgments for routing, ranking, extraction, and verification when application behavior needs semantic understanding; use as a supporting method within the current planning or delivery task.
license: MIT
---

# TypeSafe

Use TypeSafe's System One models to turn application state into typed judgments
and probabilities. Keep rules, calculations, authorization, and execution in code.
Preserve the requested stack and scope; use TypeSafe where semantic understanding
serves the task's acceptance criteria.

## Ground

Read [the contract](../../core/CONTRACT.md), repository guidance, the current
request or `.loopzero/task.md`, and the affected application boundary. Read
[the building reference](reference.md) for judgment design and composition.
Start with the [live documentation index](https://docs.typesafe.ai/llms.txt),
then read the current API or chosen SDK, relevant primitive, and closest cookbook
before writing an integration. If live docs are unavailable, report that limit
and use installed SDK types or available local docs without inventing contracts.

## Local Credentials

Follow [the shared credential convention](../../core/CREDENTIALS.md). For TypeSafe,
use `TYPESAFE_API_KEY` first, then `~/.config/typesafe/api-key` for host-side
calls to `https://api.typesafe.ai`. See
[TypeSafe setup](../../SETUP.md#typesafe-credentials-optional) for provisioning.
Resolve the key inside the calling process without printing it. Keep application
credentials out of required checks and review sandboxes. Installing this skill
does not change the SDK's credential lookup behavior.

## Fit The Existing Workflow

- Use [plan](../../core/skills/plan/SKILL.md) if the objective or acceptance is
  unclear, or [write-design-doc](../../core/skills/write-design-doc/SKILL.md)
  when a durable design contract is needed. This method does not create a
  separate task or delivery process.
- For code changes, follow [implement](../../core/skills/implement/SKILL.md).
  Record the state sent to TypeSafe, expected decisions, uncertainty handling, service-failure
  behavior, and material validation limits in the existing task and PR sections.
- Apply [security-review](../../core/skills/security-review/SKILL.md) to
  changed credentials, outbound data, or execution boundaries within the
  contract's review budget.
  Keep API keys server-side and out of prompts, fixtures, logs, and commits.

## Build And Verify

1. Work backward from observable application behavior to narrow judgments.
   Choose Choice for one option, Noul for a yes/no probability, or Score for a
   graded dimension. Supply enough evidence and a no-match path when needed.
2. Batch independent questions over shared state. Chain requests only when
   later state or options depend on earlier answers. Handle uncertainty on the
   branches actually used; model confidence is not permission to act.
3. Prove application behavior at its public boundary with deterministic service
   responses covering success, uncertainty, and service failures. Evaluate model
   quality separately on representative labeled cases; fake responses do not
   establish model accuracy or calibrate thresholds.
4. Run repository checks through `loopzero check`. Keep required checks offline
   under the existing sandbox configuration. Fetch docs during development;
   do not add live model calls, credentials, or network access to required checks
   merely to validate an integration. Report separately any live evaluation
   performed and any remaining quality uncertainty.

## Exit

The requested decisions compose into the expected application behavior, failure
and uncertainty paths are explicit, and evidence distinguishes deterministic
checks from model-quality evaluation. Follow the owning task through review and
`loopzero ready`; merge only when separately requested.
