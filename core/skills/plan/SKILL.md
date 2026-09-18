---
name: plan
description: Bound a requested change with objective, acceptance criteria and affected paths before implementation, when those are not yet clear.
---

# plan

Read [the contract](../../CONTRACT.md). Turn a request into a change small
enough for one PR.

## Do

1. State the objective in one paragraph. If the request contains two
   independent outcomes, split it into two tasks.
2. Write acceptance as observable conditions: a test that passes, a command
   whose output changes, a page that renders. No adjectives.
3. List the paths you expect to touch and the checks from `workflow.toml`
   that exercise them. Add a new check only if no existing one covers the
   behavior.
4. Decide routine implementation choices yourself from the code you can read.
   Ask only when a decision would change the acceptance criteria or touch a
   surface someone else owns.
5. Write the result into `.loopzero/task.md` under Objective and Acceptance
   (see [HANDOFF](../../HANDOFF.md)). No separate plan file.

## Stop when

- The objective needs a decision from the requester. Name the exact decision
  and the options; continue with any preparation that does not depend on it.
- The change cannot fit one PR. Propose the split and the order.

## Do not

- Plan extra review rounds or tracking documents. The contract already
  fixes one primary review plus one delta.
- Plan work outside the requested change to "clean up while here".
