# Long-Term `loop-zero` Optimization & Hardening Roadmap

**Status:** Dated synthesis record (`2026-11-25`). Its rules bind only where
`core/CONTRACT.md` or `docs/DELEGATION.md` carries them.
**Tracking issue:** `feder-positronics/loop-zero#552` (closed 2026-11-25)
**Scope:** Multi-horizon hardening, latency reduction, and cross-repo operating rules for `loop-zero` and its primary consumer repository and host.

## Summary

Between `2026-11-10` and `2026-11-25`, a full activity, check-catch, and deletion-gate audit found that `loop-zero`'s primary losses were not inside the CLI itself, but in host resource contention, unbatched inner-loop PR churn, high-noise local checks, and prose-as-database overhead. On 2026-11-25 the record proposed addressing the host contention between self-hosted CI runners and local check slots, mechanical telemetry, and local-state workflow delivery (`#573`).

---

## 1. Dated Evidence Baseline (`2026-11-10` → `2026-11-25`)

### 1.1 14-Day Pipeline & Host Audit (`2026-11-10` → `2026-11-24`)
- **Full-pipeline volume (primary consumer):** `985` merged source PRs (`~70.4/day`), median open-to-merge **`176.6 min`** (`7,720.3` cumulative PR-hours).
- **Repeat-cycle tax:** `344` PRs (`34.9%`) were repeat PRs on an already-touched issue; **`264` (`26.8%`)** merged within 24 hours of a prior merge on the same issue — peaking in **Goal 3b Hosted Analysis (`46.4%`, `13/28`)**, **Goal 3a Local Research (`39.7%`, `31/78`)**, and **Goal 3c Shared Research/Analysis (`33.8%`, `23/68`)**.
- **Local host contention (`heavy-slot-jobs.jsonl`, `n = 16,859` jobs excl. `jit-runner`):** `5,259.0` total host-hours, with **more time waiting in FIFO queues (`2,766.6h`) than running (`2,492.4h`)**. `loopzero-check` fired `2,520` times (`2.56` runs per merged PR; `1,940.3h`, only `52.9%` OK); Playwright `browser` jobs spent `78.6%` (`1,289.9h / 1,641.0h`) queued.
- **Pre-PR check catch & deletion-gate audit:**
  - `0` verified heavy-DB-only pre-first-push catches across all `8` traced integration-failure artifacts (`3` post-draft product regressions, `4` harness/flake failures, `1` already-pushed rerun). Survivorship bias remains open because overwritten `checks.json`, removed worktrees, and generic journal exits prevent a full pre-push census.
  - `native_pair.py` / `--sol-astra-pair`: **`0` observed uses** across `4,421` submitted PR reviews (`4,308` consumer + `113` `loop-zero`) and `1,586` archived receipts.
  - `receipt_archive.py`: **`0` external readers** (`1,586` files, `69.5 MB` allocated); `eligibility.evaluate()` reads no local receipts, while archive failures during merge cleanup leaked task clones on disk.

---

## 2. Operating rules agreed on 2026-11-25

These rules record the Owner + cross-model (Neon + Fable) consensus of 2026-11-25. Current obligations come from `core/CONTRACT.md` and `docs/DELEGATION.md`; a rule below binds only where one of them carries it:

1. **Two-Lane Rollout (Merge-Gated, Non-Retroactive):**
   - *Fast lane (backward-compatible fixes, speedups, relaxations, deletions):* merge to `loop-zero` `main` and refresh hosts/pins **ASAP (same hour)**.
   - *Quiet-window lane (contract-tightening or eligibility-breaking changes):* because `main` is linear, **hold tightening PRs unmerged until a 24–48h quiet window** so they never sit ahead of urgent fast-lane fixes.
   - *Non-retroactive enforcement:* proposed evidence-envelope versioning plus warn-first stderr output in `loopzero ready` (today the CLI accepts only version 1 markers). Add **zero date cutoffs or state** to `eligibility.py`.
2. **Mechanical Dynamic Priority (No Fixed Ratios, No LLM ROI Scoring):**
   - Under the `N=10` WIP cap, dispatch uses one mechanical rule: **anything blocking `main` or `≥3` open PRs goes first; otherwise strategy outcome work**. Do not impose a static product/infra split (e.g., 80/20) or subjective LLM ROI scores.
3. **3-Speed Adaptive Control Loop (Fit for `~70 PRs/day`):**
   - *Real-time (zero-LLM, edge-triggered):* exactly **4 tripwires** writing one issue/file line on state edge:
     1. `scratch-retention` reclaimed `0 B` in 24h while `/` `>70%`.
     2. Oldest queued `heavy-slot` job `>90 min`.
     3. `main` red, or oldest Mergify queue entry `>60 min`.
     4. Consumer `loop-zero` pin behind `main` with `≥1` open PR failing on it.
   - *Daily telemetry pulse:* report daily, **act only on 3-day rolling moves (`≥10 pts`)** — single-day `50–70` PR samples carry `±9 pts` binomial noise.
   - *Weekly / experiment-end:* reserved strictly for structural contract changes or scheduled multi-day readouts.
4. **Thin-Airlock Delivery Boundary:**
   - Keep `loop-zero` a thin shipping airlock preserving cross-family AI review (verified `80%` exclusive bug catch rate) and exact/ancestor review eligibility (`#562`, `#566`).
   - Do not add inner-loop commands or garbage collectors to `loop-zero`. Inner loops run in consumer worktrees/drafts or workflow experiments (`#573`) and graduate clean `≤400` non-test LOC slices.

---

## 3. Horizon 3 proposals

### Horizon 3 (Post-`2026-12-01` — Portability & Local-First Delivery)

| Item | Tracking | Purpose |
|---|---|---|
| **Local-state workflow delivery experiment** | `loop-zero#573` | Deliver one feature through a T3 Workflow run keeping iteration state local, publishing to GitHub only when ready. |
| **Repo portability cleanup** | `loop-zero#524` | Finish moving remaining consumer specifics out of `loop-zero` and universal mechanisms into it. |
| **Self-improvement retro lean-out** | `loop-zero#537` | Align retro cadence and artifacts with the 3-speed control loop and versioned-doc / GitHub-issue model (`#551`). |
| **Strictness freeze** | `core/CONTRACT.md` | Add no new eligibility rules or round caps unless removing a verified failure mode or reducing end-to-end cycle time. |

---

## 4. Explicit Cuts & Non-Goals

- **Cut:** fixed `80/20` product/infra quota and subjective LLM ROI scoring.
- **Cut:** `loopzero gc` or CLI inner-loop orchestration commands (`loop-zero` stays a shipping airlock).
- **Cut:** per-PR stall alerts, context/token tripwires (hooks already exist), schedule-count alerts (fixed once on the host), and real-time KPI percentage alerts.
- **Cut:** skipping cross-family AI review on reverts (keep exact-SHA review; trivial verification via regenerating `git revert --no-commit <culprit>`).
- **Cut:** destructive deletion of historical `~/.local/state/loopzero/receipts/` files (`~69.5 MB` left untouched on disk).
