---
name: diagnose
description: Establish the cause of a newly observed failure with one small reproduction and discriminating evidence; use fix-failing-tests for an understood failing test.
---

# Diagnose

Read [the contract](../../CONTRACT.md). Produce a demonstrated cause and the
smallest repair and validation contract. Stop before implementation.

## Do

1. Capture the expected and observed behavior, environment, data source, and
   exact command and output. Start with one small, reliable reproduction: one
   test, command, or input. Prefer an assertion or command that fails for the
   defect and would pass after repair. Do not report local or staging state as
   production.
2. Check whether a missing tool, network, sandbox, or stale dependency could
   explain the failure. If a configured check exercises the reproduction,
   `loopzero check` shows whether it reproduces in that sandbox; it does not
   rule out environmental causes elsewhere.
3. For an intermittent failure family, find when its rate changed and what
   landed then. Compare a measurable proxy, such as duration, query size,
   allocations or failure rate, across commits around the onset. Aggregate the
   evidence already captured for every failure, such as stack dumps, timings
   and logs, before adding instrumentation. Treat "unrelated" or
   "infrastructure" as a hypothesis to test, not a starting classification.
   Run disposable experiments in one investigation worktree; batch any hosted
   instrumentation into one delivery per set of hypotheses.
4. If several explanations remain, rank hypotheses with evidence for and
   against each. Choose an experiment that separates the leading explanations,
   then rerank after each result. Confirm the cause by predicting and observing
   a result that distinguishes it from alternatives.
5. Return the reproduction, ranked and rejected hypotheses, discriminating
   evidence, confirmed cause, blast radius, smallest repair and validation
   contract, and remaining uncertainty. Route an understood test repair to
   [fix-failing-tests](../fix-failing-tests/SKILL.md) and other code changes to
   [implement](../implement/SKILL.md).

Use synthetic or local data first. Redact credentials, tokens, database URLs,
personal data, and user content before persisting or quoting evidence. Add
temporary instrumentation only when authorized and necessary; record it, avoid
secret-bearing output, and remove it before exit. Do not edit production code,
restart remote services, or mutate remote data without explicit authority.
Any production-derived experiment must be explicitly in scope and name its
cleanup or recovery contract before it runs.

## Stop when

- You cannot reproduce or prove the cause. Report what you tried, the exact
  missing evidence or authority, and what remains uncertain.
- The repair would change behavior outside the task. Write it up; do not
  start it.
- A shared gate keeps failing while the cause is unproven. Report its failure
  rate and the deliveries it blocks to the requester as a decision, for example
  a temporary retry or quarantine, instead of holding everyone's work on proof.

## Do not

- Treat correlation or a plausible explanation as proof.
- Declare an intermittent failure fixed on fewer passing runs than its prior
  failure rate warrants: at a 30% failure rate, two passes happen about half
  the time by chance.
- Apply speculative patches to see if the symptom goes away.
- Commit, tag, or push from a reproduction script; reproductions are
  read-only against Git.
