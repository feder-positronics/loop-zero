# Long-Term `loop-zero` Optimization & Hardening Roadmap

**Status:** Active synthesis record (dated `2026-11-25`)
**Tracking issue:** `feder-positronics/loop-zero#552`
**Scope:** Multi-horizon hardening, latency reduction, and cross-repo operating rules for `loop-zero` and its primary consumer (`feder-positronics/intelflo` + `eowca/host-config`).

## Summary

Between `2026-11-10` and `2026-11-25`, a full activity, check-catch, and deletion-gate audit showed that `loop-zero`'s primary losses were not inside the CLI itself, but in host resource contention, unbatched inner-loop PR churn, high-noise local checks, and prose-as-database overhead. Horizon 1 hardening is largely complete (`#550`, `#551`, `#558`, `#559`, `host-config#145`, `intelflo#14181/#14183/#14194/#14237/#14239`). Horizons 2–3 focus on closing the newly exposed `danio-3` self-hosted runner vs. `heavy-slot` contention (`host-config#171`), shipping zero-LLM tripwires and a 3-day-rolling daily pulse (`host-config#148`), routing mechanical schedules to Codex (`host-config#172`), evaluating the `2026-12-01` Day-7 readouts (`intelflo#14237`, `#14243`), and testing local-state workflow delivery (`#573`).

---

## 1. Dated Evidence Baseline (`2026-11-10` → `2026-11-25`)

### 1.1 14-Day Pipeline & Host Audit (`2026-11-10` → `2026-11-24`)
- **Full-pipeline volume (`feder-positronics/intelflo`):** `985` merged source PRs (`~70.4/day`), median open-to-merge **`176.6 min`** (`7,720.3` cumulative PR-hours).
- **Repeat-cycle tax:** `344` PRs (`34.9%`) were repeat PRs on an already-touched issue; **`264` (`26.8%`)** merged within 24 hours of a prior merge on the same issue — peaking in **Goal 3b Hosted Analysis (`46.4%`, `13/28`)**, **Goal 3a Local Research (`39.7%`, `31/78`)**, and **Goal 3c Shared Research/Analysis (`33.8%`, `23/68`)**.
- **Local host contention (`heavy-slot-jobs.jsonl`, `n = 16,859` jobs excl. `jit-runner`):** `5,259.0` total host-hours, with **more time waiting in FIFO queues (`2,766.6h`) than running (`2,492.4h`)**. `loopzero-check` fired `2,520` times (`2.56` runs per merged PR; `1,940.3h`, only `52.9%` OK); Playwright `browser` jobs spent `78.6%` (`1,289.9h / 1,641.0h`) queued.
- **Pre-PR check catch & deletion-gate audit:**
  - `0` verified heavy-DB-only pre-first-push catches across all `8` traced integration-failure artifacts (`3` post-draft product regressions, `4` harness/flake failures, `1` already-pushed rerun). Survivorship bias remains open because overwritten `checks.json`, removed worktrees, and generic journal exits prevent a full pre-push census (`intelflo#14236`).
  - `native_pair.py` / `--sol-astra-pair`: **`0` observed uses** across `4,421` submitted PR reviews (`4,308` IntelFlo + `113` `loop-zero`) and `1,586` archived receipts.
  - `receipt_archive.py`: **`0` external readers** (`1,586` files, `69.5 MB` allocated); `eligibility.evaluate()` reads no local receipts, while archive failures during merge cleanup leaked task clones on disk.

### 1.2 Live Trial & P0 Readouts (`2026-11-24` → `2026-11-25 13:00Z`)

| Track / Issue | State (`2026-11-25`) | Verified Result |
|---|---|---|
| `loop-zero#534` (in-place coordinator resets) | Open (repeat check `14:00Z`) | **7× longer** median coordinator generation (`~14m → ~100m`, `v519–v532`); retired threads `v512–v517` archived with `0` bound schedules. |
| `loop-zero#550` (`#554`, `#555`) | **Closed** | Atomic `checks.json`, SHA-checked idempotent cleanup, retired empty adjudication registries, Codex version hard-fail replaced with observational metadata. |
| `loop-zero#559` (`#560`) | **Closed** | Deleted `native_pair.py` and `receipt_archive.py` (`+63/-2,091` lines); amended `core/CONTRACT.md` (`...until completion`); left `69.5 MB` legacy archives untouched on disk. |
| `loop-zero#558` (`#562`, `#566`) | **Closed** | Approved and resolved `request_changes` primary reviews carry forward across clean `ahead` base merges, eliminating base-merge PR close-and-reopen cascades. |
| `loop-zero#551` (`#553`, `intelflo#14245`, `host-config#147`) | **Closed** | Retired `od13` prose digest (`99` versioned doc rules, `6` ruling issues, `83` expired, `0` disputed); `0` live schedule prompts reference the digest; `lz-lean-ledger.md` cut to `<1 KB`. |
| `host-config#145` (disk burn) | **Closed** | Root cause found (`logs/HEAD` reflog mtime reset 574/800 worktrees, stalling `scratch-retention`, plus hard-linked `.venv`/`node_modules`). `/` recovered from `361G (74%)` to **`451G free (67%)`**. |
| `host-config#146` (schedule hygiene) | Open (Actions 1–3 done) | All `407` archived-thread schedule bindings disabled (`0` left); `3` mislabeled `— Codex` schedules retitled to `— Opus`; `bin/role-model-meter` merged (`#152`). |
| `intelflo#14181` & `#14186` (paid CI) | **Closed** | First 24h Blacksmith direct meter (`11-24 11:55Z → 11-25 11:55Z`): **`0` jobs, `$0.00 USD`** (target `≤$3/day`). |
| `intelflo#14183` (dev-first QA, `#14314`) | **Closed** | 24h before/after: builder share of slot runs **`76.23% → 82.96%`** (`1,097 → 2,391`), builder run time **`74.85% → 83.21%`**, QA run time `10.33% → 6.94%`. |
| `intelflo#14194` (Trial P1 local-check split) | **Merged** (`#14365` fix) | Live since `2026-11-25 02:25Z` (defers 7 heavy local stages; `45/45` `100%` candidate baseline). |
| `intelflo#14239` (`AGENTS.md` draft hygiene) | **Closed** | Concise `## Before the PR` rules landed (draft iteration, daily base merge, `≤400` non-test LOC slices). |
| `intelflo#14237` (WIP cap `N=10`) & `#14243` (LR inner loop) | Live → `2026-12-01` readout | Baseline: `25.26%` outcome-serving merges, `191m` med open-to-merge, `86.23%` Mergify 1st-pass. LR confirmed `collection_smoke.py` already runs worktree-direct; brief updates moved to `#13749` comments. |

### 1.3 New Bottleneck Surfaced (`2026-11-25 12:12Z–12:57Z`)
Moving PR CI off Blacksmith (`#14186`) while adding Vitest/ESLint/Prettier to the Mergify Queue Quality Gate (`#14236`) eliminated paid CI cost (`$0.00/day`) but exposed **uncoordinated host saturation on `danio-3`**:
- GitHub self-hosted runner jobs pushed `danio-3` to **load `71–81.9` on 36 cores** outside `heavy-slot`'s visibility (load stayed `81.9` even after `heavy-slot` drained to `0` live jobs).
- Mergify candidate `#14429` timed out at the 20-minute Queue Quality Gate limit during Vitest, requiring an emergency OD ruling to cap `danio-3` local checks from `6 → 3`.
- Tracked in **`eowca/host-config#171`** (`p0`).

---

## 2. Settled Operating & Governance Rules

These rules represent Owner + cross-model (Neon + Fable) consensus and govern ongoing co-development of `loop-zero` and consumer repositories:

1. **Two-Lane Rollout (Merge-Gated, Non-Retroactive):**
   - *Fast lane (backward-compatible fixes, speedups, relaxations, deletions):* merge to `loop-zero` `main` and refresh hosts/pins **ASAP (same hour)**.
   - *Quiet-window lane (contract-tightening or eligibility-breaking changes):* because `main` is linear, **hold tightening PRs unmerged until a 24–48h quiet window** so they never sit ahead of urgent fast-lane fixes.
   - *Non-retroactive enforcement:* use evidence-envelope versioning plus warn-first stderr output in `loopzero ready`. Add **zero date cutoffs or state** to `eligibility.py`.
2. **Mechanical Dynamic Priority (No Fixed Ratios, No LLM ROI Scoring):**
   - Under the `N=10` WIP cap (`intelflo#14237`), dispatch uses one mechanical rule: **anything blocking `main` or `≥3` open PRs goes first; otherwise strategy outcome work**. Do not impose a static product/infra split (e.g., 80/20) or subjective LLM ROI scores.
3. **3-Speed Adaptive Control Loop (Fit for `~70 PRs/day`):**
   - *Real-time (zero-LLM, edge-triggered):* exactly **4 tripwires** (`host-config#148`) writing one issue/file line on state edge:
     1. `scratch-retention` reclaimed `0 B` in 24h while `/` `>70%`.
     2. Oldest queued `heavy-slot` job `>90 min`.
     3. `main` red, or oldest Mergify queue entry `>60 min`.
     4. Consumer `loop-zero` pin behind `main` with `≥1` open PR failing on it.
   - *Daily telemetry pulse:* report daily, **act only on 3-day rolling moves (`≥10 pts`)** — single-day `50–70` PR samples carry `±9 pts` binomial noise.
   - *Weekly / experiment-end:* reserved strictly for structural contract changes or scheduled multi-day readouts.
4. **Thin-Airlock Delivery Boundary:**
   - Keep `loop-zero` a thin shipping airlock preserving cross-family AI review (verified `80%` exclusive bug catch rate) and exact/ancestor review eligibility (`#562`, `#566`).
   - Do not add inner-loop commands or garbage collectors to `loop-zero`. Inner loops run in consumer worktrees/drafts (`intelflo#14239`, `#14243`) or workflow experiments (`#573`) and graduate clean `≤400` non-test LOC slices.

---

## 3. Horizon 2–3 Execution Roadmap

### Horizon 2 (`2026-11-25` → `2026-12-01` — Stabilize Host/CI, Measure, Decide)

| Priority | Item | Owner | Exit / Guardrail |
|---|---|---|---|
| **H2-1 (`p0`)** | **`eowca/host-config#171`** — Coordinate `danio-3` self-hosted GitHub Actions runners with `heavy-slot` admission (cgroup/core partitioning or shared load-aware admission) + Vitest timeout relief in Queue Quality Gate (`intelflo#14236`). | Lean System + Delivery Coordinator | `danio-3` stays below saturation under concurrent candidates + local checks; zero 20m Queue Quality Gate timeouts. |
| **H2-2 (`p0`)** | **`eowca/host-config#148`** — Ship daily telemetry pulse (3-day rolling) and the 4 edge-triggered zero-LLM tripwires. | Lean System | Daily pulse live before Trial P1 graduation; zero steady-state agent wakes. |
| **H2-3** | **`eowca/host-config#172`** — Route the 3 mechanical recurring schedules (`nightly CI`, `delivery sweep`, `What's new`) from Opus to Codex. | Lean System | Verified via `bin/role-model-meter` over 24h toward `<500` Opus coordinator/warden req/day. |
| **H2-4** | **`intelflo#14236` (item 2) & `#14194` Trial P1 graduation** — Complete pre-P1 historical local-check catch census and monitor candidate quality. | Lean178 + Delivery Coordinator | **Hard revert guard:** revert P1 local-check demotion if 3-day rolling Mergify 1st-pass drops `>5 pts` below `86%`. |
| **H2-5** | **`loop-zero#534` closeout** (`2026-11-25 14:00Z` repeat measurement) and **`loop-zero#572`** (GitHub App 404 on uninstalled repos). | Lean System / `loop-zero` | Close `#534` if merges remain inside normal weekly band; fix `#572`. |
| **H2-6** | **Day-7 Outcome Readouts (`2026-12-01`)** on `intelflo#14237` (WIP cap) and `intelflo#14243` (Local Research inner loop). | Delivery Coordinator | Targets: med open→merge `≤90m`, Mergify 1st-pass `≥93%`, `<24h` repeat PRs `≤15%`, outcome-serving share `≥50%`, revert rate `≤1.0%`. |

### Horizon 3 (Post-`2026-12-01` — Portability & Local-First Delivery)

| Item | Tracking | Purpose |
|---|---|---|
| **Local-state workflow delivery experiment** | `loop-zero#573` | Deliver one feature through a T3 Workflow run keeping iteration state local, publishing to GitHub only when ready. |
| **Repo portability cleanup** | `loop-zero#524` | Finish moving remaining IntelFlo specifics out of `loop-zero` and universal mechanisms into it. |
| **Self-improvement retro lean-out** | `loop-zero#537` | Align retro cadence and artifacts with the 3-speed control loop and versioned-doc / GitHub-issue model (`#551`). |
| **Strictness freeze** | `core/CONTRACT.md` | Add no new eligibility rules or round caps unless removing a verified failure mode or reducing end-to-end cycle time. |

---

## 4. Explicit Cuts & Non-Goals

- **Cut:** fixed `80/20` product/infra quota and subjective LLM ROI scoring.
- **Cut:** `loopzero gc` or CLI inner-loop orchestration commands (`loop-zero` stays a shipping airlock).
- **Cut:** per-PR stall alerts, context/token tripwires (hooks already exist), schedule-count alerts (fixed once in `#146`), and real-time KPI percentage alerts.
- **Cut:** skipping cross-family AI review on reverts (keep exact-SHA review; trivial verification via regenerating `git revert --no-commit <culprit>`).
- **Cut:** destructive deletion of historical `~/.local/state/loopzero/receipts/` files (`~69.5 MB` left untouched on disk).

---

## 5. Open Questions & Blocking Dependencies

1. **Historical pre-PR catch rate (`intelflo#14236` item 2 — Owner: Lean178):** Required before permanently graduating Trial P1 (`#14194`) beyond its guarded trial window.
2. **Runner vs. slot partitioning on `danio-3` (`host-config#171` — Owner: Lean System):** Blocks scaling back `danio-3` local-check admission from `3 → 6` without risking Mergify Queue Quality Gate timeouts.
3. **Day-7 WIP cap & Local Research outcome proof (`intelflo#14237`, `#14243` — Owner: Delivery Coordinator, due `2026-12-01`):** Determines whether `N=10` holds, adjusts, or lifts, and whether Goal 3 repeat PR share dropped from `39.7%` to `≤15%`.
