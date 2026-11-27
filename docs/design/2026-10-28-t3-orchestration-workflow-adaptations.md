# T3 orchestration and workflow adaptations

Date: 2026-10-28. Status: exploration and ranked proposals. The requester
commissioned periodic upstream-reading documentation and this synthesis; the
remaining proposals are not implementation assignments or scheduled work.

Upstream: [current architecture](../ARCHITECTURE.md),
[delivery contract](../../core/CONTRACT.md),
[delegation guidance](../DELEGATION.md), and
[upstream review procedure](../T3-UPSTREAM.md).

## Evidence and limits

Research covered the complete transcript of Theo's
[These 3 skills changed my life](https://www.youtube.com/watch?v=5Bmp9cz1JN8),
selected onscreen examples, and all nine skills in his
[published source](https://github.com/t3dotgg/tstack/tree/1085050f3baed1ac724caa4a8166145fb2ac88af/skills).
The [first published versions](https://github.com/t3dotgg/tstack/tree/6efae364201abc9d9f07443e9caa461f42e51090/skills)
were compared with that revision. Published companions, including council and
later testing/activation changes, are not necessarily wording spoken in the
video or exact copies of the personal fleet checkout shown there.

Official T3 sources were inspected at
[5211acfe](https://github.com/pingdotgg/t3code/tree/5211acfe95a0c6d82a60e2524b5e46b47fe67a3e).
Version facts and later comparisons belong to [T3 upstream review](../T3-UPSTREAM.md).

Repository conclusions were checked against loop-zero at 4ccc0efa, including
the [handoff](../../core/HANDOFF.md),
[implementation skill](../../core/skills/implement/SKILL.md),
[issue safeguards](../../core/skills/work-issue/reference.md),
[status implementation](../../src/loopzero/cli.py), and
[setup](../../SETUP.md#connect-your-agent). This is not a live consumer audit.
No delivery-delay audit or skill benchmark was found here; earlier unsupported
references to those are withdrawn. These research proposals are not PR review
findings; delivery findings remain in their owning PR threads.

## Decisions and ownership

- **D1 — Locked:** add the upstream-reading procedure under existing maintenance
  ownership. Weekly reading during active use supplements event-driven review;
  it introduces no unattended recurring job or permission to upgrade.
- **D2 — Locked:** retain current review, sandbox, attribution, queue and readiness
  boundaries. Source precedence describes evidence for runtime claims, not a new
  instruction hierarchy or authority grant.
- **D3 — Assumed:** the order below favors observed gaps, small scope and
  observable outcomes. Benefits and effort are qualitative hypotheses, not
  measured returns or model-vote scores.
- **D4 — Deferred:** implementation of R3, R6 and R7 requires a separate commission or
  an already-authorized task matching the proposed experiment. No proposal
  claims a worker, issue, release window or recurring schedule.

## Ranked proposals

R1 (periodic upstream review), R4 (compact delegation handoff) and R5
(inherited-work relevance) have shipped; R2 and R8 are moot. The open proposals
follow.

### R3 — Trial one acceptance-to-observation example

Why: proof is already required, but readers should distinguish demonstrated
behavior from convincing completion prose without reconstructing a conversation.
How: on the next authorized observable change, its parent records acceptance,
tested revision, pre-fix failure, post-fix result and unobserved conditions with
their next-action owner. Use the existing Notes or human PR Validation prose;
keep evidence where already owned, without maintaining duplicate records.
Acceptance: another reader identifies proved and outstanding acceptance, and
checks refresh preserves human evidence outside generated markers. The owning
implementation/review skills and `core/HANDOFF.md` are possible later edit paths
only if the trial demonstrates a missing example. Risk: stale or private evidence
and extra forms. This prose-only documentation task is not a regression trial.

### R6 — Target activation, composition and revision ambiguity

Why: a workflow name, an instruction to stop, and quoted source text can select
different behavior. Portable skills also depend on coherent installed references.
How: after a demonstrated ambiguity, the skill/host maintainer exercises a small
positive and negative case against the actual harness and consumer instructions.
Examples: invoked Full Stop versus ordinary stop; composed `work-issue` with an
explicit readiness limit; mismatched CLI/source/skill revisions. Relevant paths
are the owning skill, catalog and setup guidance, not a new benchmark framework.
Acceptance: the intended workflow and authority boundary are respected with the
tested version disclosed. Risk: treating a text-matching test as proof of model
behavior, or inventing a recurring evaluation program without a real problem.

### R7 — Offer public comment attribution only where a consumer needs it

Why: authorship trailers and App review identity do not necessarily explain an
agent-written comment posted through a person's account.
How: the consumer maintainer may choose a concise on-behalf-of note in its
comment guidance. Keep problem-first PR narratives and private-data inspection;
do not add repetitive callouts to generated evidence or invent model identity.
Acceptance: a reader can distinguish agent wording from the account owner's
personal statement without mistaking it for backend attestation. Depends on a
concrete consumer requirement and posting authorization. Risk: noise and false
provenance claims. No general CLI change is proposed here.
