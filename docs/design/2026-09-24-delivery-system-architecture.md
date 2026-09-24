# Delivery system architecture: feedback, writes, identity, failures

Date: 2026-09-24. Status: accepted (owner decisions 2026-09-24).

This note treats loop-zero and a consumer repository as one system: agents,
the `loopzero` CLI, GitHub as state store and event bus, policy and product
workflows, Blacksmith runners and the Mergify queue. It records why cost,
GitHub quota exhaustion, slow delivery and queue fragility have shared causes,
the principles that address them, and who owns each remaining gap. Evidence is
from IntelFlo, the first consumer on the two-step CI topology.

## System

```text
agent sessions (Claude, Codex, T3 panels)
  └─ loopzero: start → check → pr → review → ready → merge
       └─ GitHub PR (body, comments, reviews, labels)   ← state store and event bus
            ├─ policy workflows: PR Policy, Loop-zero Eligibility, review event
            ├─ product CI (Blacksmith)
            └─ Mergify queue → candidate PR → policy + product CI again → main → nightly
shared resources: one user's GraphQL quota, runner minutes, reviewer budgets,
local test database, and each repository's CI/queue configuration
```

## Failure today: four coupled loops

Measured in IntelFlo, 2026-09-23 23:55 → 2026-09-24 06:10 UTC, 18 merges.

1. **Late feedback multiplies work.** `loopzero check` ran no tests, so the
   merge queue was the first test run. 23 of 42 candidates failed (55%). Each
   candidate is itself a PR (34 of the last 50 PRs were candidates), so every
   failure re-runs eligibility, PR Policy, product CI, Mergify comments and
   labels, and every waiter behind it. Failed candidates cost $2.11 of $3.64.
   When a path-filtered suite is skipped on a candidate, a regression can land
   on main and then fail every later candidate: #4870 skipped integration
   (services were outside the risk filter) and broke an RSS query budget that
   failed three other PRs' candidates before it was found.
2. **Every state write is an event.** loopzero records state on the PR (checks
   block, queue markers, review findings, replies). Each write re-runs trusted
   workflows: 681 eligibility, 472 PR Policy and 149 review-event runs for 18
   merges, about $2.57 or 40% of spend, 227 of them cancelled but billed.
3. **One identity, no budget.** Sessions, loopzero, review publishing, direct
   `gh` calls and T3 panels share one account's 5,000 GraphQL points per hour.
   Exhaustion failed a queue-confirmation comment and dequeued a valid PR
   through a candidate-policy HTTP error.
4. **Transient errors become verdicts.** Fail-closed is correct at the trust
   boundary, but infrastructure errors, stale markers and ordering races
   (eligibility computed before the review is posted) currently produce
   dequeues and failed gates. Each costs a manual retry and more load.

### Evidence

IntelFlo is private; links need repository access. Counts come from the
Actions API (`actions/workflows/<file>/runs?created=<window>`, jobs per run,
per-job minutes rounded up at Blacksmith list rates) and the Pulls API.

- Candidate failures and cost: 42 `ci.yml` runs on `mergify/merge-queue/`
  branches in the window, 23 failed; failed-run minutes priced at $2.11.
- Skipped integration let regressions land: #4870's candidate
  [run 35958087838](https://github.com/feder-positronics/intelflo/actions/runs/35958087838)
  and #4950's candidate #4956
  [run 35968380650](https://github.com/feder-positronics/intelflo/actions/runs/35968380650)
  skipped `Full integration suite`; `git bisect` names #4870 for the RSS
  budget failure (for example
  [run 35962095494](https://github.com/feder-positronics/intelflo/actions/runs/35962095494))
  and #4950 for `test_question_edit_supersedes_stale_plan_requirements_and_judgments`
  ([run 35975878765](https://github.com/feder-positronics/intelflo/actions/runs/35975878765)).
- Policy-workflow volume: 681 `eligibility.yml`, 472 `pr-lint.yml`,
  149 `review-event.yml` runs; sampled billed minutes per run 0.70, 0.85, 1.0.
- Transient errors as verdicts: `GitHub API request failed: HTTPError` in the
  candidate policy dequeued #4824
  ([run 35928549364](https://github.com/feder-positronics/intelflo/actions/runs/35928549364));
  GraphQL exhaustion broke a queue-confirmation comment, which loop-zero
  [#223](https://github.com/feder-positronics/loop-zero/pull/223) moved to REST.

## Principles

| # | Principle | Meaning |
|---|---|---|
| P1 | Each layer decides one thing, as cheaply as possible | Local check: every deterministic lane (lint, types, unit tests, related frontend tests), free and offline. Candidate: integration truth on the exact landing tree, for every change that can affect it. Nightly: breadth, and it must alert. No layer repeats another's job. |
| P2 | Write only on state change; subscribe to transitions | loopzero writes only changed state. Workflows trigger on head, base, review and resolution transitions and filter irrelevant events before a runner is allocated. |
| P3 | One identity per role, each with its own budget | Automation (loopzero, review publishing) uses a GitHub App installation token; people and T3 use their own account. |
| P4 | Waiting costs almost nothing | One waiter per PR; REST reads, conditional requests (a `304 Not Modified` does not count against the primary rate limit), backoff with jitter. |
| P5 | Transient is not terminal | Bounded retries for 5xx, 429 and quota errors at every trust-boundary call; verdicts still fail closed; an infrastructure error never dequeues. |
| P6 | One owner for shared infrastructure | A repository's CI, queue and workflow configuration change through one coordinator; this note is the reference topology consumers adopt. |
| P7 | Steer by five numbers, computed from GitHub on demand | No new state or ledger; see Metrics. |

## Current state and gaps

| Principle | Delivered | Open gap | Owner |
|---|---|---|---|
| P1 | Product suites on candidates only (IntelFlo #4811, #4840, #4845). In the merge queue on 2026-09-24: integration on every backend candidate and one PR per candidate (#4938), local unit and related-frontend lanes (#4952), nightly retry/alert for every job (#4954) | Measure first-pass rate; fix the Playwright content-detail failures that time out nightly | IntelFlo coordinator |
| P2 | PR body rewritten only when it changes | Trim policy-workflow triggers (keep review/thread and base transitions; skip title-only edits) after the hosted eligibility pin upgrade | Session owning GitHub transport (d48663c8) |
| P3 | — | GitHub App for automation; `review_publishers` and eligibility trust must accept its bot login | Owner action, then loopzero change |
| P4 | REST PR reads and queue comments (#220, #223, #225) | Conditional requests in waits | Session owning GitHub transport |
| P5 | Parent-batch fallback (#218) | Retries in candidate policy and eligibility reads; admission-pending vs removed in `merge --wait` | Session owning Mergify integration |
| P6 | — | Record the coordinator per consumer repository | Owner |
| P7 | — | A consumer-side report of the five metrics | IntelFlo coordinator |

## Metrics

Computed weekly from the GitHub Actions and Pulls APIs for merged PRs in the
window. Targets are the owner's 2026-09-24 goals, not measured guarantees.

| Metric | Definition | Target | 2026-09-24 overnight |
|---|---|---|---|
| Candidate first-pass rate | candidates whose first product run succeeds / candidates | ≥ 85% | 45% |
| Cost per merge | runner minutes × rate for CI and policy workflows / merged PRs | ≈ $0.06 | ≈ $0.36 |
| Workflow runs per merge | policy + CI workflow runs / merged PRs | trend down | ≈ 79 |
| GraphQL points per hour | `rateLimit` samples, by identity once P3 lands | no exhaustion | exhausted twice on 09-23 |
| Merge latency | ready → merged, p50 and p90 | trend down | not yet measured |

## Decisions (owner, 2026-09-24)

- Adopt P1–P7 and the first-pass rate as the steering metric; local checks may
  take a few minutes longer (target ≤ 10 minutes total).
- Separate automation identity (P3).
- One infrastructure owner per consumer repository (P6).
- Disable the ChatGPT Codex GitHub connector's automatic reviews in IntelFlo:
  it posts a usage-limit comment on every PR; `loopzero review` is the review.

## Non-goals

No local daemon, queue ledger, global `gh` wrapper or new loopzero gate. Each
gap above is a bounded change in the component that already owns the concern.

## Open questions

- GitHub App scope and review provenance: which permissions the app needs and
  how existing eowca-published reviews stay valid during the switch. Owner;
  blocks the P3 loopzero change.
- Batching: re-enable pairs only when measured first-attempt pair failures are
  well below 50%. IntelFlo coordinator.
