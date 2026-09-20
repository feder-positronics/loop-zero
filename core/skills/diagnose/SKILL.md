---
name: diagnose
description: Establish the cause of broken behavior by reproduction and evidence before choosing a repair.
---

# diagnose

Read [the contract](../../CONTRACT.md). Use this when something fails and
the cause is not obvious. The output is a demonstrated cause and the
smallest repair route, not a fix.

## Do

1. Reproduce with the smallest case you can: one test, one command, one
   input. Record the exact command and output, and which environment and
   data source it ran against; do not report local or staging state as production.
2. Decide whether the failure is environmental (missing tool, network,
   sandbox, stale dependency) or behavioral (the code does the wrong thing).
   Run the reproduction through `loopzero check` to rule out the first.
3. When more than one explanation fits, design a step that separates them
   and run it. Do not pick the most familiar story.
4. Deliver: the cause, the reproduction, the evidence that rules out the
   alternatives, and the smallest repair. Hand the repair to
   [implement](../implement/SKILL.md).

## Stop when

- You cannot reproduce. Report what you tried, what is missing (data,
  credentials, hardware) and what remains uncertain.
- The repair would change behavior outside the task. Write it up; do not
  start it.

## Do not

- Treat correlation or a plausible explanation as proof.
- Apply speculative patches to see if the symptom goes away.
- Commit, tag or push from a reproduction script; reproductions are
  read-only against Git.
