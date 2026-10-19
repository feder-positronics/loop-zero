---
name: diagnose
description: Prove the cause of a newly observed failure; use fix-failing-tests when the cause is understood.
---

# Diagnose

Follow [the contract](../../CONTRACT.md). Return a demonstrated cause and the
smallest repair and validation contract; stop before implementation.

1. Capture expected and observed behavior, environment, data source, exact
   command and output. Find one small, reliable reproduction that fails for the
   defect and would pass after repair. Distinguish local, staging and production
   evidence.
2. Check missing tools, network, sandbox and stale dependencies. When a configured
   check exercises the reproduction, `loopzero check` tests that sandbox only;
   it cannot exclude environmental causes elsewhere.
3. For intermittent failures, find when the rate changed and what landed then.
   Compare failure rate or another measurable proxy across commits around the
   onset. Aggregate captured logs, dumps and timings before adding instrumentation.
   Test “infrastructure” or “unrelated” as hypotheses. Use one investigation
   worktree for disposable experiments; batch hosted instrumentation per set of
   hypotheses.
4. Rank competing explanations with evidence for and against each. Run an
   experiment that separates the leaders, then rerank. Confirm the cause by
   predicting and observing a result that distinguishes it from alternatives.
   Correlation and symptom disappearance are insufficient; avoid speculative
   patches.
5. Return reproduction, ranked and rejected hypotheses, discriminating evidence,
   cause, blast radius, smallest repair, validation and uncertainty. Route proven
   test repairs to [fix-failing-tests](../fix-failing-tests/SKILL.md), other changes
   to [implement](../implement/SKILL.md).

Use synthetic or local data first. Redact secrets, database URLs, personal data
and user content before quoting or persisting evidence. Temporary instrumentation
needs authority, must avoid sensitive output, and must be recorded and removed.
Production code edits, remote restarts and remote data mutations require explicit
authority; production-derived experiments must be in scope with cleanup or recovery
defined beforehand. Reproduction scripts must not commit, tag or push.

If reproduction or proof is unavailable, report attempts and the exact missing
evidence or authority. Stop at out-of-scope repairs. For a shared gate blocked
by an unproven cause, report its failure rate and blocked deliveries for a requester
decision about retry or quarantine.

Never declare an intermittent failure fixed on insufficient passing runs:
at a prior 30% failure rate, two passes occur about half the time by chance.
