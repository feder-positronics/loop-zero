# Deterministic delivery: design assumptions and practices

Date: 2026-10-07. Status: architecture rationale, dated delivery/comparison evidence
and separately marked proposed adaptations. The contract owns operational policy;
recorded comparison acceptance does not establish formal review or verified delivery.

Upstream: [current architecture](../ARCHITECTURE.md),
[delivery contract](../../core/CONTRACT.md), and
[September delivery-system evidence](2026-09-24-delivery-system-architecture.md).

## Purpose and source limits

Contributors and session controllers need a durable explanation of why loop-zero
puts delivery rules in inspectable code while using models for semantic work.
Success means a proposed change can name its invariant, evidence owner and
transition proof without rebuilding that rationale. Operational details remain
in the contract; historical measurements remain dated observations.

David Khourshid's [“Goodbye Slop; Welcome Determinism”](https://www.youtube.com/watch?v=1rMgw0Q5MgY)
(Callstack Agent Conf 2026) prompted this synthesis. The available official
description and chapters describe the difficulty of inspecting, testing and
trusting rules hidden in long agent prompts; an email example separates fixed
workflow rules from generation. Chapters identify explicit state machines
(08:07), inspectable structure (11:42), evaluations (12:21), agent-assisted
improvement (13:23), and a create/observe/improve/compare cycle (16:24).
Transcript retrieval failed, so these are description-level paraphrases, not a
verified account of the talk's detailed arguments. The repository practices
below are our synthesis, grounded in current sources rather than attributed
to the speaker.

Laurie Voss's [“The Death of the Code Review”](https://www.youtube.com/watch?v=_mi3alkqy4s)
adds the question of whether the evidence establishes a change worth shipping.
The conference's [timestamped transcript](https://ai.engineer/talks/_mi3alkqy4s-death-code-review-what-data-actually-says)
was inspected, including review quality (11:11), context beyond tests (19:30),
reviewer manipulation (20:29), and production feedback (22:00). Its practical
direction is to engineer and assess the review system. The application to
loop-zero below retains the existing review contract. Numerical claims require
their original study's population, metric and version; see the evidence limits.

## Existing authority and state

**D1 — Locked: code owns admission; models supply semantic judgments.**
The contract's commands and guards decide whether delivery advances. Models
interpret intent, implement changes and produce genuine review findings through
the [native runners](../../src/loopzero/runners.py). A model's confidence, private
review or successful session cannot replace a required check or current-head
review. Independence is the default, with a disclosed same-family fallback when
no independent family can run. Prompt-only orchestration
is the strongest alternative: it is flexible, but the deciding premise is that
delivery constraints need repeatable, inspectable enforcement. Consequence:
semantic output enters the existing evidence protocol, rather than inventing
permission to merge.

Determinism here means a policy produces the same decision from the same
observations, configuration and time inputs. It does not make model findings,
GitHub scheduling or future observations predictable. Prompts explain the task;
executable guards enforce the delivery rules they can observe. Instructions
such as truthful attribution still require contributor and reviewer judgment.

**D2 — Locked: reuse the existing authorities.** Git owns commits and ancestry;
GitHub owns live PR, review, finding and merge evidence; consumer CI and branch
protection enforce hosted gates. Local task context, check receipts and saved
runner results are disposable execution artifacts, not a second history.
A workflow database could simplify queries, but the contract settles the
premise: authoritative state already exists. Consequence: improvements compose
with these stores; no daemon, ledger or framework dependency follows from this
note.

The six commands are a useful delivery sequence, not six mutually exclusive
states. [CLI readiness](../../src/loopzero/cli.py) combines overlapping dimensions:

| Dimension | Observation and guard |
| --- | --- |
| Source | Local head, cleanliness, pushed head, configured target and ancestry |
| Local proof | Successful current-head receipt with configured commands |
| Review | Eligible current-head runner evidence and blocking finding threads |
| Hosted checks | Required checks on the authored head, including missing or pending results |
| Integration | Base compatibility or verified source-preserving queue configuration |
| Landing | Queue/merge observations, verified landed SHA, then cleanup |

A PR can be marked ready to activate CI while required checks still block
admission. An approved review can coexist with a failed check. Queue admission
can precede landing. Collapsing these into one linear status would hide guards
and uncertainty. Local readiness includes its check receipt and source CI;
[hosted eligibility](../../src/loopzero/eligibility.py) evaluates trusted source-head
review evidence without executing source code or consuming local receipts.
They are related proofs with different owners, not interchangeable verdicts.

## Transition and recovery practices

**D3 — Locked: bind evidence to identity and revalidate before effects.**
[Checks](../../src/loopzero/sandbox.py) and review recheck head and dirtiness after
execution; eligibility rereads the PR head; merge recomputes readiness and
[GitHub transport](../../src/loopzero/github.py) verifies landing. The alternative
is to trust an earlier green snapshot. The deciding premise is that source and
hosted evidence can change independently. Consequence: a changed authored head
needs renewed proof under the contract, and a source review does not prove a
consumer queue's integration tree.

For each transition, state the trigger, source identity, guard, effect and
completion evidence. For example, a readiness read may permit a draft-to-ready
write; that write can schedule asynchronous CI, so the waiter observes the same
head again within its original deadline. It cannot guarantee an atomic snapshot
across GitHub scheduling. Preserve missing, stale and failed observations until
the owning rule permits waiting or fresh evidence resolves them.

**D4 — Assumed: external notifications are hints requiring reconciliation.**
The [dated T3 verification](#t3-verification-snapshot--october-5-2026)
records at-least-once mailbox delivery and watcher limits. Notifications should
wake the controller to reread authority, rather than directly authorize a
transition. Trusting event delivery is simpler; the premise would change only
with a verified runtime guarantee covering the external effect, not merely a
message ID. The session/runtime owner must reverify this assumption when adopting
a new version; loop-zero still owns its admission gates.

Use one bounded waiter per PR, retain visible uncertainty and stop on command
errors. Make repeated execution safe where the owning operation supports it:
changed-only PR writes reduce workflow events; saved review publication identity
allows bounded read-back after a lost response. Review recovery confirms exact
live evidence before returning a saved verdict and does not automatically
repeat an uncertain POST. These are specific recovery guarantees, not blanket
exactly-once delivery. Follow the contract for retry classes, deadlines and
explicit reposts rather than copying their values here.

## Ownership, assurance and evaluation

T3 and agent sessions own interaction, delegation and workspace orchestration.
Loop-zero owns checks, formal review, readiness and verified merge. Consumer
repositories own tests, workflow scheduling, queue integration, runner capacity
and shared infrastructure. A resource lease or browser lock belongs at the
consumer operation boundary; adding it to delivery readiness would move
ownership without proving product correctness.

Readiness is a checklist, not a tamper-proof boundary: repository writers can
alter evidence. Checks fail closed if their sandbox cannot run. Review sandboxes
bound readable data while allowing required network access, and therefore cannot
prevent exfiltration of readable material. Claims of assurance must preserve
these limits and the hosted enforcement owner.

Evaluate workflow correctness separately from model quality. A useful transition
proof asserts both the allowed effect and the forbidden effect under head drift,
missing evidence, duplicate observation, lost replies and reordered checks.
Existing [CLI](../../tests/test_cli.py), [readiness](../../tests/test_github.py) and
[publication recovery](../../tests/test_review_recovery.py) tests already cover
many such paths. Semantic review quality still needs representative defects and
acceptance judgments; a correct transition cannot prove that a model found every
bug. Compare workflow improvements through ordinary reviewed changes, keeping
the invariant and owning proof visible.
Use focused, sanitized observations as test fixtures; existing PR outcomes can
show whether a change reduces substantive rework. A successful replay proves
behavior for its inputs, not a current live approval or universal model quality.

## Review quality and delivered value

**D8 — Assumed: a valid delivery verdict can still miss a product defect.**
Existing gates establish specified evidence, not complete correctness. The
alternative is to treat green checks and a schema-valid model approval as a
complete quality judgment. The deciding premise is that tests and reviewers
have limited coverage; reassess the harness when known failures expose that
coverage. Consequence: improve the criteria and evidence without relaxing
admission or treating additional reviewer agreement as independent proof.

Use these practices when designing a change or assessing the review harness:

- **Measure accepted delivery.** Compare completed objectives, substantive
  repair, time through checks/review/merge, and observed regressions. Lines,
  commits and PR counts measure activity. Even a landed change is only a proxy
  for product value; the consumer owns evidence of useful deployed behavior.
  Compute bounded observations from existing PRs and consumer telemetry, with
  defined denominators; this introduces no metrics store or automatic target.
- **Make acceptance wider than test passing.** State behavior, affected callers,
  scope, regression obligations, owning proof and architectural constraints.
  Link domain context such as external consumers, scheduled jobs and migration
  guarantees in task context before implementation. The existing
  [plan](../../core/skills/plan/SKILL.md) and
  [code-review](../../core/skills/code-review/SKILL.md) methods cover much of this.
- **Preserve reviewability.** Keep one cohesive objective and split unrelated
  work before generation. Review chunking manages context size; it does not
  restore missing cross-file understanding. Use the plan skill's existing scope
  guidance rather than deriving a universal line cap from a study. Generated
  migrations still need explicit compatibility and safety obligations.
- **Require justified findings.** Investigate suspicious paths, then describe
  the failing condition, affected caller and consequence. Inspect the owning
  proof and surrounding code rather than trusting the author's explanation.
  A focused reproduction belongs in authoring/check work when it is useful;
  the native reviewer is read-only, and the Claude adapter exposes only read
  tools. A bootable-app harness and automatic repair are not part of the native
  review protocol.
  Repairs return to the authoring workflow and require renewed checks and review.
- **Evaluate the reviewer as a component.** Check known defects and valid
  changes, disagreement, severity, unsupported findings and hostile text in
  the material being reviewed. A deterministic parser test proves protocol
  handling, not semantic detection or manipulation resistance. Resolution
  feedback is useful but cannot establish precision or recall by itself: an
  accepted suggestion may be wrong, and an unresolved report may be correct.
- **Keep deployment feedback owned.** Consumers monitor relevant regressions,
  migrations, security and operational behavior, with a named response and
  rollback owner. Verified merge proves landing, not successful deployment or
  useful operation. An escaped defect can motivate an independent owning
  regression test and a reviewed harness improvement. Findings remain in their
  original PR threads; fixture examples are synthetic or class-level proofs,
  not a second archive of findings or private agent transcripts.

The current [runner prompt](../../src/loopzero/runners.py) supplies task context,
the diff, output constraints and class-wide finding guidance. It does not embed
the full code-review skill automatically. Aligning their substantive criteria
is a candidate change, not a shipped property. Preserve the contract's review
budget and independence policy while improving review quality; vendor examples
of repeated passes or repair agents do not authorize additional formal rounds.

Source files, comments and task narrative can contain attempts to influence
the reviewer. The existing read-only sandbox bounds effects and access; output
validation bounds the protocol. Neither establishes prompt-injection resistance
or absence of shared reviewer blind spots. Treat task narrative as proposed
acceptance and inspect it against repository authority. Hardening and adversarial
evaluation are separate work, not assurance supplied by this note.

### Evidence limits

- The productivity paper's [June author summary](https://cepr.org/voxeu/columns/writing-code-versus-shipping-code-productivity-effects-across-generations-ai-coding)
  reports more-than-sevenfold code volume and 20% more releases for synchronous
  agents, while 30% more releases belongs to cumulative adoption across three
  generations. It identifies downstream stages beyond review. The talk's
  headline combines estimates; it is not a loop-zero speedup prediction.
- [METR's maintainer study](https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/)
  supports a gap between test passing and acceptance. It used four maintainers,
  three repositories and historical patches; agents had one attempt without
  feedback, and maintainers lacked live CI and ignored test requirements. It
  does not establish a universal rejection rate or equal-condition comparison.
- [Cognition's FrontierCode announcement](https://cognition.com/blog/frontier-code)
  distinguishes weighted rubric scores from passing every blocker and uses
  different task sets. The talk's benchmark comparison was not verified from
  that primary source, so it is not used as architectural evidence here.
- [Cursor's resolution-rate account](https://cursor.com/blog/bugbot-learning)
  labels public-repository comments using a model judge to decide whether they
  were addressed before merge. Our inference is to treat that as feedback about
  action taken, rather than ground truth about defects or reviewer recall.
- [Anthropic's security reviewer documentation](https://github.com/anthropics/claude-code-security-review#security-considerations)
  explicitly limits its prompt-injection assurance. That is evidence about its
  action, not a measured attack rate for loop-zero. Our runner's assurance must
  be established against its own inputs, permissions and evaluations.

### Reviewer investigations — October 7, 2026

Three bounded local investigations (16 synthetic valid/defective cases, a matched
permission follow-up and a rerun at the then-current upstream prompt) exercised
both native reviewer routes. Both approved every valid case. The one repeatable
miss was a permission defect approved when the task prose carried a forged owner
override, and the newer skepticism guidance did not reliably prevent it. The
samples were small, synthetic and agent-assessed: they show override-associated
suppression in that fixture, not general recall, ranking or injection resistance.
That result led to the R3 instruction transport compared below.

### Code audit outcome — October 7, 2026

The audit of `origin/main@b980669971d372f3ff1e31160ab10772379a7cd5` proposed four
slices, all since merged:

- R1 [#281](https://github.com/feder-positronics/loop-zero/pull/281) pins reviewer
  runtime authority and protects native state mounts, so a reviewer cannot read or
  alter host CLI state it does not need.
- R4 [#282](https://github.com/feder-positronics/loop-zero/pull/282) separates
  requested from CLI-reported reviewer settings, so publication never claims an
  unobserved model or effort.
- R2 [#283](https://github.com/feder-positronics/loop-zero/pull/283) adds the
  environment-auth route that omits host state binds (see below).
- R3 [#284](https://github.com/feder-positronics/loop-zero/pull/284) moves fixed
  reviewer policy into the native instruction channels and keeps task text and diff
  as review material, answering the override-associated suppression above.

Two limits stayed
open: a file-backed Claude bind still exposes the whole configuration directory,
and readiness still matches rendered blocker strings (D5 below). The
[architecture guide](../ARCHITECTURE.md) describes the code as it is now.

## Deferred adaptations

These illustrate separate bounded tasks, not an approved backlog. Each needs
owner agreement and evidence before adoption.

| ID / status | Candidate, alternative and deciding evidence | Owner / adoption proof |
| --- | --- | --- |
| D5 / Deferred | Typed blockers alongside rendered reasons. Today `github.Readiness` carries strings and CLI `_pending_checks` matches exact messages; retaining strings avoids an interface change, while typed blockers let transition decisions survive changes to explanations. | CLI/readiness maintainer; preserve gate verdicts and human output while tests show wording changes cannot alter waitability. |
| D6 / Deferred | `status --json` derived from existing observations. Current `cmd_status` emits prose; consumers could continue invoking commands without parsing status. | CLI/runtime integration owner; one identified consumer, explicit identity/uncertainty schema, and no new authority or store. |
| D7 / Deferred | Generated event sequences or fixture replay alongside handwritten tests. More handwritten cases are simpler; generated paths are useful when event ordering and combinations are the behavior under test. | Owning module maintainer; cover a known race or interaction, use an independent oracle to detect invalid effects, and justify any new test dependency. |
| D9 / Deferred | Align substantive acceptance, scope, regression and proof criteria across the review skill and native runner prompt. Existing task context may suffice; choose alignment when representative cases expose a missed criterion. | Review maintainer; keep the output schema, budget and independence rules, and assess correct and faulty changes rather than only snapshotting prompt text. |

Agents may propose these improvements; normal task acceptance, implementation,
checks and formal review decide adoption. Until then, existing behavior remains
the baseline and this note requires no migration or rollback.

## Runtime authentication boundary — October 7, 2026

The implementation slices inspected the installed Claude Code 2.1.291 and Codex
0.160.1 binaries and their versioned source, without credentials or model calls.
Environment authentication (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`) lets
the reviewer run with disposable private state, so it omits the host configuration
and global-state binds. File-backed authentication keeps those binds, which expose
the whole Claude configuration directory and `~/.claude.json`; Codex keeps its
`auth.json` bind. Source supports in-place writes for file-backed auth, but none of
this proves token refresh visibility on the host or general filesystem privacy.
The [architecture guide](../ARCHITECTURE.md#components-and-boundaries) owns the
current rules.

## R3 native comparison — October 7, 2026

The accepted baseline was `7d248a8295ca22c5e0074e8288539a19e8ae6c86`; the frozen
production candidate was `0d9325e673184d1945f73a79275c9671ac885ccb`. Cases,
behavioral oracle, source/runtime/configuration hashes and balanced ordering were
frozen before execution. The matched phase used both arms across four permission
conditions and four repetitions (32 Claude calls). An independent Astra agent
assessed every parsed response and accepted the matched gate before the 64-call
suite of sixteen frozen valid/defective cases across both configured routes and
both arms. It then independently inspected every suite response and accepted the
declared gate for formal delivery. This was agent-only adjudication, not a human
audit or a `loopzero review` verdict.

| Matched Claude outcome | Baseline | Candidate |
| --- | --- | --- |
| Concrete defect detected, control | 4/4 | 4/4 |
| Concrete defect detected, forged override | 0/4 | 4/4 |
| Valid control approved without blockers | 4/4 | 4/4 |
| Valid override approved without blockers | 4/4 | 4/4 |

| Suite outcome | Baseline | Candidate |
| --- | --- | --- |
| Claude defective cases detected | 7/8 | 8/8 |
| Codex defective cases detected | 8/8 | 8/8 |
| Claude valid cases approved without blockers | 8/8 | 8/8 |
| Codex valid cases approved without blockers | 8/8 | 8/8 |

There were zero paired-baseline defect losses, zero valid-case blockers and zero
core adjudication ambiguities. Injection-only objections and unrelated findings
did not count as product-defect detection. Extra out-of-scope blockers on already
defective cases were reported separately: matched baseline 1/candidate 2, suite
Claude baseline 4/candidate 2, and suite Codex 0 in either arm. Unsupported
ancillary claims about historical behavior, roles, duration and security purpose
persisted. These results neither erase that noise nor prove general reviewer quality.

All 96 native invocations completed successfully with 96 distinct sessions;
each fixture context remained unchanged and actual source, runtime and access
guards passed. Fresh independent Git repositories exposed only allowlisted case
source and their own objects. Labels, oracle/rationale, prior outputs, research
directories, parent checkout and unrelated Git objects were inaccessible. No
actual host history was inspected or exported. Both arms used Claude's existing
OAuth environment with private HOME and zero auth binds, while Codex retained its
existing live auth.json file bind and disclosed local-state limitation. Normal
active accounts, providers, authentication and billing routes were unchanged;
neither backend was independent.

The actual selected Claude was 2.1.293, SHA-256
`8968405e26db478af44eabc4635ab5ca557057b702a54460a59c13e1b253e978`.
Codex was 0.161.0, SHA-256
`9a820c17865fa825d04db416818679a9d63bd72e50835c396f496e5684626c9c`.
Requests were Opus 5.5 medium and Astra 6 medium. CLI evidence reported Claude
Opus 5.5 for all 64 Claude calls, with effort unknown; model and effort were
unknown for all 32 Codex calls. These are qualified CLI observations, not backend
attestation. Missing optional observations introduce no eligibility gate.

The sample is small, synthetic and drawn from convenience cases. It supports the
declared permission detection improvement and Codex no-regression gate only;
it does not prove general injection immunity, real-world mergeability or an
end-to-end delivery bypass boundary. The candidate arm moved fixed reviewer
policy into native instruction channels; instruction files from the reviewed head
stayed enabled in both arms. R3 merged as
[#284](https://github.com/feder-positronics/loop-zero/pull/284); disabling reviewed-head
instruction files and skills came later as separate hardening. D5–D7 remain deferred.


## T3 verification snapshot — October 5, 2026

The pinned T3 sources showed two boundaries that still shape delivery. The
[notification mailbox](https://github.com/pingdotgg/t3code/blob/37de6cbde65c7cf9ba90a2557c232e63b7e16988/apps/server/src/orchestration-v2/NotificationMailbox.ts)
permits at-least-once delivery, so act only after rereading
Git, GitHub and the current head, and keep repeated execution idempotent. A
[PR watch](https://github.com/pingdotgg/t3code/blob/37de6cbde65c7cf9ba90a2557c232e63b7e16988/apps/server/src/orchestration-v2/PullRequestWatchReactor.ts)
is only a hint (terminal merged/closed observations end it without a wake),
so one `loopzero merge --wait` owns landing verification and cleanup, and a watch
never replaces rerunning the waiter. Version-specific T3 facts belong to the
[T3 upstream review](../T3-UPSTREAM.md).
