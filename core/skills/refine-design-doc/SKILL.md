---
name: refine-design-doc
description: Converge an existing design artifact through review, decision resolution, focused edits, and validation until no critical or important design findings remain.
---

# Refine Design Doc

Refine one existing exploration, ADR, initiative, blueprint, or design document
until it is ready for its intended next action. Edit the artifact, but do not
implement the design.

## Grounding

Read the complete artifact, linked issue or `.loopzero/task.md`, upstream
decisions, repository guidance, and enough current code to verify its premises.
Locked decisions remain constraints unless new evidence invalidates their
premise; assumptions remain active and vetoable.

When this is part of a loop-zero task, make the edits in its task worktree. Do
not create a second worktree or a separate findings file.

## Converge

Repeat only while material findings remain:

1. Apply `review-design-doc` to the current artifact and surface all `critical`
   and `important` findings material to its intended next action.
2. Resolve response-only findings here against the artifact's evidence and
   constraints. Use `resolve-findings` only for findings already recorded as PR
   review threads. Ask only when a choice is requester-reserved or changes
   acceptance, scope, cost, or an external commitment.
3. Make focused edits for the accepted responses. Preserve source-of-truth links
   instead of copying their detail, and do not turn suggestions into blockers.
4. Re-read the edited artifact and verify the underlying finding, not merely its
   wording, is resolved.

Choose the edit order and combine tightly coupled findings when useful. If the
same important finding survives two passes, revisit the chosen approach. Stop
after five non-converging passes and report the surviving blocker rather than
forcing nominal closure.

## Validate And Exit

Verify links, frontmatter, current-state claims, decision status, open-question
owners, acceptance, and the artifact's repository conventions. Run
`loopzero check` when the document belongs to a loop-zero task.

Exit when no `critical` or `important` finding remains, settled decisions stayed
settled unless evidence formally reopened them, open questions are durable and
properly blocking, and the artifact is actionable for its named next actor. Put
the artifact path and material decisions in `.loopzero/task.md` Notes when that
task file exists.
