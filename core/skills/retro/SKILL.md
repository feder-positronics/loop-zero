---
name: retro
description: Judge recent workflow changes and choose at most one next change in the weekly retro.
---

# Weekly workflow retro

Lean commissions one fresh read-only analyst using the repository's `deep`
profile on the same weekday. Run early once if a guard is outside its band
two days running or independent incidents repeat.

## Evidence

- Run [retro_metrics.py](../../../scripts/retro_metrics.py) once with the
  commissioned repo, fixed UTC window end, outcome, order and p0 labels,
  agreed stall threshold, and available receipt and host inputs. Save JSON
  stdout to scratch. Take numbers only from the script and query fields needed for decisions.
  Compare the last seven days with the prior seven and the four-week trend.
  Lead with outcomes closed and order-to-acceptance time. Name the source of
  each `UNKNOWN`. Replay with the same end and frozen inputs.
- Use `weekly_sample.items` directly. Explain any shortfall below six from the
  report. Read each sampled case once for its wait, who could move it and first
  real consumer use or missing proof. Use a sampled contrary case to test the
  apparent constraint when available. Read additional records only to settle
  a verdict or test that constraint. Route defects to the owning issue via
  [diagnose](../diagnose/SKILL.md) in the same turn.

## Two decisions

1. Give each recent change **keep**, **revert** or **still-open** against its
   recorded rule and review date. Separate installation, first real consumer
   use and measured effect. Keep if comparable evidence meets the target
   with both guards intact. Revert for a lost protection, material regression
   under comparable load or no benefit after adequate exposure. For insufficient
   evidence, name the next observation and date. Extend once, then escalate
   unresolved verdicts to Lean.
2. Name a deletion candidate or explain why none qualifies. Choose at most
   one change for the evidenced constraint. Record it and direct owner or OD
   orders on `workflow-change` issues with a script target, two script guards,
   review date, revert action and install marker. Recording must not delay orders.

For structural changes, reverts or safeguard removals, Lean commissions a
short read-only check using the same evidence and a different family's
configured [deep route](../../../docs/DELEGATION.md#resolve-the-model-before-delegation).
Select Anthropic for the default OpenAI analyst. Adopt on agreement or defer
with the disputed fact.

Send Marcin one “Since you were away” screen in plain language with Warsaw
dates. Put up to two owner decisions first under a heading, each with a
recommendation and default.
