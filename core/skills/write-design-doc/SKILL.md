---
name: write-design-doc
description: Research and write an exploration, ADR, initiative, blueprint, or guide when implementation needs a durable design contract; do not use to review an existing design or implement it.
---

# Write Design Doc

Produce the smallest design artifact that lets the next actor proceed without
re-deriving intent or architecture. Do not implement the design.

## Before Writing

- Read the request, issue or `.loopzero/task.md`, repository guidance, related
  design artifacts, and enough code to verify the current state.
- Treat requester decisions as settled, distinguish assumptions from facts, and
  verify every claim that an existing component provides a capability.
- Align material ambiguity before drafting. A locked decision stays settled, an
  assumption remains vetoable, and a deferred decision becomes an open question.
- When the document is a repository change, work in the task worktree created
  by `loopzero start`.
- Skip the artifact when the request is clear, small, and safely implementable
  as one bounded change.

Read [the design-doc reference](reference.md) before choosing the artifact and
drafting its contract.

## Write

Use the repository's existing documentation location, frontmatter, terminology,
and template conventions. Link to sources of truth instead of copying them.
Where no convention exists, choose a descriptive Markdown path and state why in
`.loopzero/task.md` Notes.

Keep open questions in the artifact with an owner and the decision they block.
For user-facing work, write scenarios before proposing UI, then validate material
UI shape with a mockup or an existing pattern. For implementation blueprints,
name concrete paths and sequenced acceptance without prescribing inferable
coding steps.

## Validate And Exit

- Remove placeholders and verify links, frontmatter, premises, and the documented
  procedure where the artifact is a guide.
- Run `loopzero check` when the document is part of a loop-zero task; fix any
  failure before handoff.
- If material ambiguity remains, revise the artifact or leave an explicit
  blocker; do not label it implementation-ready merely because drafting ended.
- Exit with a commit-ready document whose acceptance and open questions are
  actionable. Put its path and material decisions in task Notes so the commit
  and PR body hand the contract to planning or implementation.
