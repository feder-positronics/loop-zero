---
name: retro
description: Judge recent workflow changes and choose at most one next change in the weekly retro.
---

# Weekly workflow retro

## Evidence

- Run [retro_metrics.py](../../../scripts/retro_metrics.py) once with the
  commissioned repo, fixed UTC window end, the outcome and order (`p0`) labels its AGENTS.md names,
  agreed stall threshold, and available receipt and host inputs. Save JSON
  stdout to scratch. Take numbers only from the script and query fields needed for decisions.
  Compare the last seven days with the prior seven and the four-week trend.
  Lead with outcomes closed and order-to-acceptance time. Name the source of
  each `UNKNOWN`.
- Use `weekly_sample.items` directly. Explain any shortfall below six from the
  report. Read each sampled case once for its wait, who could move it and first
  real consumer use or missing proof. Use a sampled contrary case to test the
  apparent constraint when available. Read additional records only to settle
  a verdict or test that constraint. Name each defect's owning issue for a
  [diagnose](../diagnose/SKILL.md) referral.

## Two decisions

1. Give each recent change **keep**, **revert** or **still-open** against its
   recorded rule and review date. Separate installation, first real consumer
   use and measured effect. Keep if comparable evidence meets the target
   with both guards intact. Revert for a lost protection, material regression
   under comparable load or no benefit after adequate exposure. For insufficient
   evidence, name the next observation and date. Extend once, then escalate
   unresolved verdicts to the commissioner.
2. Name a deletion candidate first or explain why none qualifies. Choose at
   most one change for the evidenced constraint under
   [Workflow health](../../../docs/DELEGATION.md#team-roles), or none when the case is weak.
   Give its owning issue, script target, two script guards, review date,
   revert action and install marker.

Return the verdicts, defect referrals and at most one proposed change to the
commissioner, who records them and issues any orders.

The commissioner runs the retro early once if a guard is outside its band two
days running or independent incidents repeat. For structural changes, reverts
or safeguard removals, it commissions a short read-only check on the same
evidence from a different family's [deep route](../../../docs/DELEGATION.md#resolve-the-model-before-delegation),
and adopts on agreement or defers with the disputed fact.
