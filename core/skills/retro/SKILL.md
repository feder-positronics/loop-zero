---
name: retro
description: Judge recent workflow changes and choose at most one next change in the weekly retro.
---

# Weekly workflow retro

Lean commissions one fresh read-only analyst using the repository's `deep`
profile on the same weekday. Run early once if a guard is outside its band
two days running or independent incidents repeat.

## Evidence

- Run [retro_metrics.py](../../../scripts/retro_metrics.py) with the commissioned
  `--repo`, fixed UTC `--window-end`, outcome, order and p0 labels, and configured
  receipts and host inputs where available. Pass the agreed `--stall-hours`
  threshold to include stalled p0s. Compare the last seven days with the
  previous seven and use the four-week trend for context. Lead with outcomes
  closed and order-to-acceptance time.
- Take all numbers from the script. Read the selected issues, PRs and change
  records directly for qualitative evidence. Keep missing data as `UNKNOWN`
  and name the missing source.
- Use `weekly_sample.items`. The script reserves two seeded merged controls,
  two slowest orders accepted that week and the longest stalled p0 when
  available, then takes up to four oldest open orders from the order input
  up to eight distinct cases total. The seed derives from the repository and
  report end. Replay with the same end and frozen inputs. Use the pool sizes
  and shortfall to explain when the category limits or available cases leave
  fewer than six.
- For each case, identify where it waited, who could move it and the first
  real consumer use or its missing proof. Test the apparent constraint
  against a contrary case. Route defects to the owning issue through
  [diagnose](../diagnose/SKILL.md) in the same turn.

## Two decisions

1. Give each recent change **keep**, **revert** or **still-open** using its
   recorded rule and review date. Separate installation, first real consumer
   use and measured effect. Keep when comparable evidence meets the target
   with both guards intact. Revert if a protection was lost, a material
   regression appears under comparable load, or adequate exposure shows no
   benefit. Mark still-open when evidence is insufficient and name the next
   observation and date. Extend once and surface any unresolved verdict to Lean.
2. Name a deletion candidate first or explain why none qualifies. Choose at
   most one next change aimed at the evidenced constraint, including zero
   when the case is weak. Record any chosen change on a `workflow-change`
   issue with a script metric as the target, two script metrics as guards,
   a review date, a revert action and an install marker. Record direct owner
   and OD workflow orders the same way, without delaying the work.

Lean commissions a short read-only cross-family check from the `review`
profile for structural changes, reverts or safeguard removals. Adopt when
both agree or defer with the disputed fact.

Send one screen to Marcin in “Since you were away” using Warsaw dates and
plain language. Put any owner decisions first under a visible heading, with
a recommendation and default for each. Include up to two owner decisions.
