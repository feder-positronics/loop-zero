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
[5211acfe](https://github.com/pingdotgg/t3code/tree/5211acfe95a0c6d82a60e2524b5e46b47fe67a3e),
including agent instructions, tool definitions/contracts and the V2 documentation
set. The V2 index and feature-lifecycle documents explicitly include target
behavior. The MCP guide's sample lacks isolation and options present in the
contracts; examples alone cannot establish available parameters. The latest
stable release observed was
[v0.1.3](https://github.com/pingdotgg/t3code/releases/tag/v0.1.3), published
2026-10-27. This does not establish deployment of that release.

Initial upstream review, 2026-10-28: the live environment API reported server
0.1.2, while the host CLI separately reported 0.1.0. These labels do not attest
an exact build or by themselves establish incompatibility. The active session
instructions and
[v0.1.2 contracts](https://github.com/pingdotgg/t3code/blob/044f07257c9684ae4543bd1e1ebf9c394e9e3830/packages/contracts/src/orchestratorMcp.ts)
were compared with
[that tag's injected instructions](https://github.com/pingdotgg/t3code/blob/044f07257c9684ae4543bd1e1ebf9c394e9e3830/packages/provider-core/src/server/orchestrationInstructions.ts).
They support bounded task inspection/waiting; the newer upstream instructions'
different running-task guidance is not grounds to drop active host/session
rules. No runtime policy or installation was changed. The repository coordinator
owns assessing future relevant updates through the linked procedure.

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
- **D4 — Deferred:** implementation of R2-R8 requires a separate commission or
  an already-authorized task matching the proposed experiment. No proposal
  claims a worker, issue, release window or recurring schedule.

## Ranked proposals

### R1 — Adopt periodic upstream review in this documentation change

Why: compatibility evidence and examples age independently of installed behavior.
The initial version/source comparison above demonstrates that distinction.
How: [T3-UPSTREAM.md](../T3-UPSTREAM.md) names the owner, cadence, primary sources,
classification and bounded adoption procedure, linked from architecture,
delegation and the docs index. The coordinator or integration maintainer owns it.
Acceptance: a reader can identify source class, reported version, relevant delta,
next-action owner and adoption authority; local links and declared checks pass.
Risk: duplicating runtime instructions. Keep technical references version-aware
and link their owners instead of copying tool recipes into policy.

### R2 — Correct the no-PR status hint in a separate CLI task

Why: `cmd_status` unconditionally suggests `loopzero pr` when no PR exists,
although implementation requires a clean committed head and successful check
before publication. This is a verified sequencing mismatch, not an observed
production failure.
How: the CLI maintainer changes the hint to explain that prerequisite, with
owning cases in `tests/test_cli.py`; a truthful sequence may suffice without
another eligibility engine. Review architecture impact in that change.
Acceptance: dirty, unchecked and successfully checked no-PR cases receive
truthful advice; status itself grants neither publication nor merge eligibility;
existing PR status behavior is preserved. Depends on separate implementation
authorization. Risk: overpromising what one snapshot proves.

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

### R4 — Trial a compact delegation and attention handoff

Why: persistence is useful only when the authorized finish point, available
proof methods and next waiting owner survive delegation and resumption.
How: the commissioning parent uses existing Context, Verification, Integration
executor, Return and Report fields in [the brief](../DELEGATION.md#briefs-and-ownership)
to state the requester's authority source, finish boundary, any relevant proof
permission and the waiting owner. Genuine human blockers use the available
question mechanism; T3 watch cleanup belongs to the runtime handoff.
Acceptance: the child reaches the requested boundary without treating a phrase
or readiness as merge authority, reports missing proof, and leaves one named
next-action owner without duplicate watching. Source and session permissions
remain governing. Depends on a suitable authorized delegation; retain only
wording the trial shows useful. Risk: expanding an already substantial template.

### R5 — Exercise inherited-work relevance on an actual takeover

Why: collision checks and credit preservation exist; a clean rebase does not
prove the original goal remains unmet after changes since divergence.
How: the continuation owner revalidates the acceptance slice, current work and
competing deliveries before choosing continue, rework or propose closure. Keep
the original commits/credit when appropriate and obtain needed closure or
rewrite authority. Use `range-diff` as an aid when rewriting commits, followed
by behavior proof. Likely guidance owner: `work-issue` or its reference.
Acceptance: retained acceptance and any superseded work are explicit on the
existing issue/PR; no duplicate owner, lost credit or unnecessary source-head
rewrite appears. Cross-family writing still affects independent-review
eligibility. Risk: broad history inspection or an implicit permission expansion.

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

### R8 — Defer incident-history extraction with explicit prerequisites

Why: readability could improve, but historical cases are interleaved with
generally applicable fatal-event, evidence, retention and publisher-trust rules.
How, if later commissioned: the contract maintainer first supplies a relocation
map identifying each general invariant that remains and each case-specific
reference that moves; a reviewer checks every rule remains discoverable and
applicable. Retain all pinned constraints and cross-links, with normal review
and architecture validation. Acceptance: ordinary and named-recovery scenarios
reach the same governing rules after the move. Risk: accidentally weakening
normative requirements. Navigation evidence and a precise preservation map are
prerequisites, not permission to start this work now.

## Complete theme disposition

The ranks select changes; this inventory also retains ideas that are already
implemented, conditional or owned outside loop-zero. Sources below distinguish
spoken sections from published companion instructions.

| Theme and source | Current disposition and consequence |
| --- | --- |
| A. Finish ownership; Full Send/Stop, 15:23-22:16 | Readiness default and authorized merge already exist. R4 clarifies delegation; finish does not silently expand to deployment. |
| B. Scoped testing authority; Full Send companion | R4 trial only where permissions need explanation. Actual user authority can supersede lower-priority ask-first preferences; a copied skill cannot grant it. |
| C. Regression/runtime proof; 17:21 | Preserve public-seam and pre-fix failure requirements. R3 makes evidence visible; apps may run within actual sandbox/configuration limits. |
| D. Complementary models and native harnesses; 6:31-11:23 | Keep bounded read-only advice and configured routes; distinguish provider, model, options and harness. No general provider ranking, automatic strongest model or extra formal approval follows. |
| E. Minimal repairs and bounded review; Sol Loop, 5:16 | Retain primary plus one delta, class investigation and evidence-backed pushback. Reject rare-case-as-nit rules, recursive reviewers, extra rounds and findings ledgers. |
| F. Visible disagreement; 6:31-7:46 | Formal head-bound verdicts/threads already preserve objections. Distinguish approval, execution failure, dispute and exhausted budget. Trusted publisher evidence remains editable, not tamper-proof. |
| G. Earlier feedback and PR attention; 11:23-11:52; file-pr companion | Keep focused proof/checks before draft publication. No mandatory pre-PR model layer, non-draft default or blanket requirement to await every advisory bot. |
| H. Inbox and genuine human blockers; 12:44-18:xx | R4 includes question routing and safe unanswered fallback. Working, waiting and settled presentation belongs to T3; silence grants no approval. |
| I. Watches, waiters and recovery; babysit-pr companion | Preserve one owner, reconcile existing state, unwatch at human handoff, revalidate after wakes and use canonical waiters. No duplicate polling, speculative reset shortcuts or uncertain write replay. |
| J. Conservative resumption advice; inbox/resume theme | R2 fixes the evidenced no-PR hint only. A next-action hint is not an eligibility verdict or permission inference. |
| K. Activation/composition; 4:02-4:34, 24:39-28:19 | R6 is conditional. Outer user constraints survive composed skills; quoted names are not grants. Invocation/context-duplication claims need harness-version verification. |
| L. Fleet scope, pins and pruning; 2:24-4:02, 7:46, 25:00 | Setup already requires matching CLI/source revisions and complete relative references. Host owns distribution, credentials and capacity. Prune demonstrated duplication; do not impose a line-count target or copy a personal fleet layout. |
| M. Inherited goal, credit and modernization; 22:16-24:39 | R5 supplements existing ownership/collision/attribution rules. Preserve contributor credit and independent-review eligibility; do not default to replacement PRs, closure or blanket rebases, especially with source-preserving queues. |
| N. Clear narratives and public attribution; file-pr/comment companions | Problem-first descriptions fit existing handoffs. R7 is consumer-optional. Check private details and verify posted replies before resolving; prose is not native identity attestation. |
| O. Release, observation and recovery; 19:35 | Existing work-issue safeguards keep unobserved acceptance open. Consumer owns rollout, canary, rollback and observation; merge or thread settlement is not universal completion. No deployment subsystem is proposed. |
| P. Measured workflow outcomes; statistics screen around 19:42 | 27/32 counts selected human-started threads with a recorded merge and no later question call; four of five question-using threads also merged. It is not a defect-free rate. Use existing PR evidence; add no metrics database or zero-question target. |
| Q. Periodic official-source reading; T3 documentation research | R1 is commissioned. Distinguish installed, released, upstream-only, proposed and unknown; verify relevant runtime claims without making source discovery an instruction override. |
| R. Evidence-led maintenance and reuse; opening/closing and sponsor sections | Learn from actual friction, reread agent-written instructions, and prefer existing infrastructure. Vendor/course promotion supplies no concrete integration requirement or authority to add an auth service. |

The published council adds provider counts, high-effort defaults, score tables
and satisfaction loops; these are companion examples, not new loop-zero gates.
Subjective scores may describe an opinion, but cannot replace genuine review,
proof or configured routing. An advisory consult is not automatically a formal
review round, nor permission for an unlimited hidden review loop.

## Cross-discussion and next steps

Astra/medium and Fable/low first ranked the full set independently, then each
read the other's report and the parent's objections. Both accepted R1-R8 in the
final order after corrections. Astra initially placed proof ahead of the status
hint; Fable emphasized its concrete inconsistency and low scope. The final order
puts that evidenced CLI task at R2, without commissioning it in this change.

Fable withdrew the claim that all Validation is machine-owned:
[`_pr_body`](../../src/loopzero/cli.py) preserves prose outside generated markers.
Notes remains valid; do not duplicate an evidence record. Both also accepted
that differing upstream/session waiting text is version-sensitive, not a proven
live conflict, and that template growth should be tested rather than mandated.

The remaining disagreement is R8 timing. Fable favors earlier extraction because
readers repeatedly pay the navigation cost. Astra requires observed navigation
trouble before prioritizing it. The parent defers it, retaining Fable's concrete
relocation-map and reviewer-discoverability prerequisites rather than dropping
the proposal or scheduling it implicitly.

Next action now: the parent validates and delivers this documentation task to
readiness. No merge is authorized. If the requester commissions follow-up work,
use R2 as one bounded CLI task, trial R3/R4 on suitable already-authorized work,
and assess the remaining candidates only under their stated conditions. Future
tasks sharing a guidance file run sequentially or as a deliberate stack, with
observable acceptance, owning checks and the existing review budget.
