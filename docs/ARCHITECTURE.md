# loop-zero architecture

loop-zero gives people and coding agents one repeatable route from an intended
repository change to a verified merge. Its goal is small, reviewable deliveries
with early feedback and clear ownership. It composes Git, GitHub, CI and existing
agent runtimes instead of becoming another task database or agent platform.

This is the current architecture guide. The [delivery contract](../core/CONTRACT.md)
owns operational rules; [setup](../SETUP.md) owns installation; the
[module map](REWRITE-SPEC.md) owns module responsibilities. Configuration keys
belong to the [example workflow](../workflow.example.toml), their validated
[schema](../src/loopzero/config.py) and [types](../src/loopzero/types.py).
[Model preferences](../models.toml) and [delegation guidance](DELEGATION.md)
own model selection. Follow those sources when a detail changes rather than
copying their tables here.

The [deterministic delivery design assumptions](design/2026-10-07-deterministic-delivery-design-assumptions.md)
record the rationale, dated reviewer evidence and version-specific native
compatibility investigation. Their proposed changes retain their recorded status;
this guide and the delivery contract describe current behavior.

## Philosophy and authority

The smallest useful unit is one task, one branch and one draft PR. A deterministic
check gives an exit-code verdict. A model reviews the authored head, independently
by default. The [bounded owner exception](../core/CONTRACT.md#owner-approved-contributor-review-exception)
permits disclosed contributor review with reduced assurance.
Readiness combines that evidence with live checks and findings. Merge verifies
what actually landed. Missing evidence blocks progress; there is no waiver file
or unsandboxed fallback.

Git owns commits, branches and ancestry. GitHub owns the live PR narrative,
reviews, finding threads, check statuses and merge state. CI and branch protection
provide the hosted enforcement. Readiness is a checklist over these sources,
not a tamper-proof security boundary: repository writers can edit PR evidence,
and branch protection remains essential.

Local `.loopzero/task.md` is working handoff context, rendered into the PR.
The CLI also writes a head-bound checks receipt and saved review output in
`.loopzero/` for local readiness and reposting. These are disposable execution
artifacts, not a second authoritative delivery history. Local readiness requires
its current successful check receipt; hosted eligibility evaluates GitHub
review evidence using trusted base configuration. Losing local artifacts may
require rerunning checks or review, and never creates a hosted approval.
Saved runner artifacts also retain the repository, PR and actual request publisher
before publication. A lost POST reply triggers a read-only reconciliation bounded
by one 30-second transport deadline, with no read retries. Recovery regenerates
the exact review body and finding locations, checks the complete review/comment
pages and unchanged head, and rereads review evidence before returning the saved
verdict. Pending, dismissed, conflicting or changing evidence blocks recovery.
`review --repost` reconciles before enforcing the remaining publication budget;
only an explicit retry after complete absence may publish again. The existing
self-review rejection fallback to COMMENT remains a separate transport rule.

Private agent transcripts, metrics ledgers and copied findings do not belong in
this guide or in a new persistent store.

## Delivery and feedback

The six steps are implemented by the [CLI](../src/loopzero/cli.py):

1. `start` creates an isolated worktree and task branch from the configured base.
   The task file states the objective, acceptance and base SHA.
2. Implementation proves the owning behavior at the narrowest public seam.
   Focused tests start early; `check` runs the repository's deterministic checks
   in a read-only source sandbox before the change enters hosted delivery.
   Before long checks, a narrow simple-command preflight catches missing local
   scripts and prepares recognized uv dependency groups offline in that same
   sandbox. It prints progress before each command. Trusted-base commands remain
   authoritative; stale task trees need integration only when prerequisites are
   missing, preserving source-preserving queue eligibility for behind branches.
   [Setup](../SETUP.md) defines the recognized command forms and cache remedy.
3. `pr` checks the task branch's closed PR history through GitHub REST before
   pushing or writing. A confirmed merged PR completes that task branch, even
   when a newer closed or open duplicate exists; the caller must start a new task.
   Absence requires complete typed pages, bounded to 100 pages; failed, malformed,
   repeated or truncated history blocks publication. Other commands retain their
   open-first, newest-closed branch lookup. An unfinished branch creates or
   refreshes a draft PR. The local task supplies narrative; the live PR owns
   validation and review after creation.
4. `review` runs a native reviewer against the exact authored SHA and publishes
   one GitHub review with findings. Its selector excludes every recognized
   `Co-Authored-By` family from the configured-base merge-base through HEAD, for
   primary, delta and repost alike. It cannot infer contributions from prose or
   determine owner permission, so a lineage carrying every configured family's
   trailers fails closed. There is one primary and at most one delta review per lineage,
   and every changed head needs renewed review. The contract's
   [review budget](../core/CONTRACT.md#review-budget) owns authorship and budget
   rules, including the bounded
   [owner-approved contributor-review exception](../core/CONTRACT.md#owner-approved-contributor-review-exception):
   a labeled, reduced-assurance route that changes neither the selector nor the
   budget.
5. `ready` derives readiness from the unchanged head, review, blocking findings,
   target branch and required CI. Its bounded waiter observes that same head.
   After its own draft-to-ready write, `ready --wait` revalidates visible readiness
   through that waiter with the original deadline, preserving all existing gates
   and replacement evidence. Missing-run diagnosis retains its two-minute observation
   grace after activation; that grace does not extend the overall wait deadline.
   An immediate green read may still precede asynchronous
   CI scheduling: this is best-effort observation, with no unconditional delay or
   wait for a new run that might never be scheduled. Merge admission still checks
   readiness again and fails closed on later blockers. Plain `ready` and already
   non-draft PRs retain their behavior.
   When every non-success signal of a required check strictly predates the eligible
   review, `ready --wait` allows up to 15 minutes from that review for a refresh,
   including time before GitHub publishes pending. Past the ordinary two-minute
   review grace it identifies the old verdict and awaited refresh. The check still
   blocks readiness throughout; at 15 minutes it becomes a final failure again
   unless existing pending/replacement evidence independently permits waiting.
   Failures at or after the review receive no extended grace. The two-minute
   grace for missing runs and generic failures, and calls without `--wait`, retain
   their existing behavior. The command's own timeout can end waiting sooner.
6. `merge` recomputes readiness, merges or enters the configured queue, verifies
   the landed SHA and then cleans up the task branch and worktree.

The contract defines exceptions, exact exit codes, budget handling and wait
procedures. Local check success is necessary but does not prove the integration
tree that a consumer's merge queue will land. Consumer CI must test that tree
when integration can change behavior. Nightly breadth supplies additional
feedback and needs an owner who responds to failures.

## Components and boundaries

```text
person / agent session / T3 workspace
  → loopzero CLI → Git worktree and authored commits
                → sandboxed owning checks
                → native model runner (independent by default)
                → GitHub PR, reviews, findings and required checks
                      → trusted hosted eligibility
                      → consumer CI and optional merge queue
                            → verified landed commit
```

The [sandbox](../src/loopzero/sandbox.py) bounds check writes, credentials,
network access and resources. [Reviewer runners](../src/loopzero/runners.py)
bound source access while allowing the network required by model CLIs; that
network means the sandbox cannot prevent exfiltration of readable data.
At native review launch the CLI pins checks, publishers and runtime paths to one
remote PR-base revision, refusing a missing remote ref/workflow unless the operator
supplies explicit configuration. Reposts and other commands retain their fallback
policy. Runners validate the complete mount plan against private review state and
known standard/custom native state roots. Existing file-backed auth mounts remain
explicit exceptions. Claude's nonempty existing API-key/OAuth environment routes omit host
config/global-state binds and use disposable private state; empty/absent values
retain the broad config and mixed global-state exposure. The runner forwards
credentials unchanged; native Claude owns route precedence and billing. Codex's
file bind is unchanged even with an environment key. File-backed binds preserve
in-place writes, without guaranteeing host replacement visibility or shared-lock
coordination. This boundary is route-specific, not full filesystem privacy. An
implicit runtime inside state receives only the selected executable's exact read-only file
mount, while explicit runtime paths have no exception. This preserves standalone
CLI startup without exposing neighboring state; bundles needing protected siblings
require installation outside state. The contract owns this boundary and its limits.

The R3 runner separates fixed reviewer policy from supplied material
inside the existing runner adapters. A fixed ASCII paragraph and validated scope
enter Claude's append-system-prompt or Codex's developer_instructions override;
commit identity stays in the JSON material header. Explicit UTF-8 stdin carries
that header, a newline and the exact raw diff without trimming task or diff text.
The selected Codex binary's version probe accepts only source-reviewed 0.160.1 and
0.161.0; refusal uses RunnerBadOutput and existing family fallback before model
input. Unsupported Claude options follow existing native failure handling.
Native defaults, repository AGENTS.md/CLAUDE.md processing and read-only tools
remain enabled. Repository instructions remain a native authority surface, so
structural transport tests do not prove injection immunity or reviewer quality.
R1, R4 and R2 merged in #281, #282 and #283. R3's frozen 96-call native comparison
passed its bounded detection/no-regression gate after an independent Astra agent
assessed every parsed response. The matched permission override improved from
0/4 baseline detections to 4/4 candidate detections, with controls preserved and
no paired defect losses or valid-case blockers in the declared sample. Comparison
evidence is separate from, and does not replace, required current-head checks,
formal review, readiness and verified merge. The
[dated design note](design/2026-10-07-deterministic-delivery-design-assumptions.md#r3-native-comparison--october-7-2026)
owns aggregate results, exact revisions and versioned source/runtime evidence.
That small synthetic sample and agent-only assessment do not prove general
immunity, real-world mergeability or an end-to-end delivery bypass boundary.
The comparison used Claude's existing OAuth environment with private HOME and
zero auth binds; Codex retained its existing live auth.json file bind and its
local-state limitation. Normal accounts, authentication and billing were preserved;
neither backend was independent. Research orchestration and charge records remain
disposable execution artifacts, adding no production ledger or evaluation framework.

Reviewer settings are optional reporting in the existing provenance container.
Adapters retain per-call requests; publication derives CLI claims from each retained
native envelope, ignoring unqualified compatibility fields and optional reported
metadata. Claude raw output stays unchanged; normalized Codex evidence retains only
bounded recognized model/effort claims, with absent evidence reported as unknown.
Chunk merge pads missing request entries and preserves their native-call order.
Legacy model/effort fields retain their historical adapter meanings for loading;
qualified summaries never use them as execution evidence. Eligibility still checks
core native provenance, independently of optional reporting. Recovery considers the
exact historical renderer only when the saved artifact lacks requested_settings;
present malformed metadata cannot activate it. New publication/repost uses qualified
wording, while publisher/head/findings/session/publication checks remain unchanged.
[GitHub transport](../src/loopzero/github.py) owns API access and changed-only
PR writes. [Hosted dispatch](../src/loopzero/hosted.py) and
[eligibility](../src/loopzero/eligibility.py) evaluate trusted review evidence.
[Mergify](MERGIFY.md) describes optional source-preserving queue integration;
[candidate validation](../src/loopzero/candidate.py) belongs at that boundary.
Candidate attestation retries typed concurrent source metadata, Mergify membership
and main-ref drift, and landed sources still listed in lineage, up to three complete
attempts with two-second pauses. A policy exception becomes a landing race only
when a fresh REST read confirms `merged is True` at the same snapshotted head;
failed reads or other evidence preserve the original exception. A non-open source
is a race only when its REST payload confirms `merged is True`; it never passes
source attestation. Each attempt resolves identity and lineage again, snapshots
and evaluates current source policy again, and performs the existing tree verification when
parent batches are missing. No evidence or approval survives a failed attempt.
A monotonic 30-second window bounds admission of retries, with at most four
seconds of deliberate delay; caller-owned synchronous API/policy calls and an
already running tree verification (its own 120-second bound) are not preempted.
Other failures stop immediately, including source-head drift. Exhaustion raises
the last race error unchanged, and hosted eligibility publishes terminal failure;
success requires one complete successful attestation. Source-PR eligibility uses
its existing path without these retries; a merged source PR publishes nothing, so a
late refresh cannot turn its landed head red.


Agent sessions and T3 runs own interaction, execution and workspace management.
They can invoke loop-zero and use its skills; their successful run or private
review is not a loop-zero delivery verdict. loop-zero owns the delivery sequence
and its evidence. A consumer repository owns its tests, shared databases,
runner capacity, workflow triggers, queue configuration and infrastructure
coordination. An external runtime's resource ownership or event subscriptions
are not features shipped by loop-zero.

The current CLI writes PR bodies only when content changes, excluding volatile
check timing from the generated validation block. Keep refreshes idempotent:
one state transition should not become repeated writes that trigger policy CI.
The transport uses bounded retries for selected transient failures and stops on
quota errors. Waits use bounded backoff and one waiter per PR; direct `gh`
polling adds shared account load and cannot replace the wait command. A rate
limit is a waiting dependency, not permission to turn missing evidence green.

## Direction and historical evidence

The [September 24 architecture note](design/2026-09-24-delivery-system-architecture.md)
is dated evidence from a consumer repository and its accepted principles. Its
measurements, gap table and owners describe that observation window. They are
not the current implementation backlog: for example, current configuration has
trusted review publishers, transport has bounded retries, and CLI waits use
backoff. Verify a historical gap against its owning code and consumer before
calling it open or closed.

The October 5 direction keeps this guide as the current entry point, preserves
the small delivery toolkit, and makes architecture upkeep part of contribution
and review. Early owning tests, truthful model attribution, independent review
by default with the bounded owner exception, changed-only writes and explicit
ownership remain the design constraints.
The approved execution direction is T3 V2 for coordination, with loop-zero
retaining checks, formal native review under that policy, readiness and verified
merge authority. This boundary is durable; verify runtime compatibility and
deployment status when adopting or updating an external executor.

### T3 verification snapshot: October 5, 2026

At this verification, the consumer rollout was separate and T3 V2 was not live
there. The [upstream V2 release](https://github.com/pingdotgg/t3code/releases/tag/v0.0.46-nightly.20261003.2610)
documents native cross-provider `delegate_task` and lossy provider switching:
a switched provider receives selected context, not the previous provider's
native state. Prefer delegation when combining harnesses. The owner-approved
controller routine starts fresh at milestones or around 300k tokens; it does
not treat automatic compaction as assurance that intent survives.

The pinned [PR watcher](https://github.com/pingdotgg/t3code/blob/37de6cbde65c7cf9ba90a2557c232e63b7e16988/apps/server/src/orchestration-v2/PullRequestWatchReactor.ts)
supplies a hint before merge; terminal merged/closed observations end the watch
without a wake. Consequently, one `loopzero merge --wait` owns landing
verification and cleanup. The pinned [notification mailbox](https://github.com/pingdotgg/t3code/blob/37de6cbde65c7cf9ba90a2557c232e63b7e16988/apps/server/src/orchestration-v2/NotificationMailbox.ts)
explicitly permits at-least-once delivery. Our execution rule follows from that
boundary: reread Git, GitHub and the current head before acting, and make repeated
execution idempotent. Timeline message IDs do not establish external exactly-once
effects.
After approval, freeze the final approved SHA. Optional suggestions cannot
justify another edit; necessary fixes use the remaining delta or, after it is
spent, a class-wide audit and fresh lineage.

The consumer owns a production lease at the operation boundary and a host-global
browser-resource `flock`; these are external resource controls, not loop-zero
readiness gates. Trial V2 with isolated user data, project copies and ports,
a coherent SQLite backup including WAL handling after provider drain, and
preservation of custom `.git`, bare repositories and `GIT_*` guards. This is an
approved migration constraint, not an in-place installation procedure or a
claim that the consumer controls have shipped. Version-specific verification
belongs in a dated compatibility note before that rollout. Reverify these
upstream capabilities, deployed versions and deployment status when runtime or
consumer deployment changes. A new
daemon, scheduler, reminder issue or metrics ledger would add state without
improving the current authority boundary and is outside this direction.

## Keeping the guide current

Architecture review is event-driven. When changing commands, configuration,
state authority, sandbox/trust boundaries, review/readiness, queue integration,
module ownership or delivery routines, the contributor reads this guide and the
owning sources before finishing the change. Update the affected explanation
and links in the same PR, or explicitly explain why the guide remains accurate.
A reviewer checks that claim against the diff, challenges misplaced ownership
and distinguishes shipped behavior from proposals and dated observations.

Run `python3 scripts/check_architecture_docs.py [BASE]` locally. With no explicit
base it uses the merge-base of HEAD and `origin/main`. The repository's
`workflow.toml` runs the default gate. PR and merge-group CI runs enforce it
against the event's base SHA before merge. Push-to-main CI runs
`python3 scripts/check_architecture_docs.py --links-only`: squash merging can
drop authored commit trailers, so landed history cannot re-establish the
pre-merge acknowledgement. This mode still rejects broken local links and
anchors; it leaves the default local and pre-merge review gate unchanged.
`loopzero check` pins checks to trusted base configuration,
so a newly introduced check enters that command after the configuration lands
on the base. Run it directly while introducing or changing the gate. The script compares that base with the working tree, including new files.
It validates local Markdown links and anchors in this guide, the docs index,
README and AGENTS guidance without network access.

The deterministic review gate watches source-owned contracts, skill routing,
the example and actual workflow, hosted workflow files, its own checker, and
selected CLI/configuration interfaces. The checker lists the exact paths and AST
selections. It ignores Python formatting and implementation-only edits outside
those interfaces. A watched change requires a guide edit in the same comparison,
or a commit trailer in the changed range:

```text
Architecture-Review: unchanged; <concrete reason the guide is still accurate>
```

The checker requires a nonblank reason of at least 20 characters. Use a real
reason, such as a workflow dependency pin that leaves triggers,
required checks and trust boundaries unchanged. This acknowledgement lives in
Git's existing history; there is no separate review ledger. Uncommitted watched
changes need a guide update or a committed acknowledgement before the final
check. A missing base, unreadable source, malformed Python or broken local link
fails the check. CI validates repository links, not the availability or current
claims of external websites.

Automation proves that review was recorded and links resolve. It cannot prove
semantic freshness, detect every architecture change, or establish that a
reasoned acknowledgement is true. Contributor and reviewer responsibility also
covers implementation changes outside the watched public surfaces. Changes to
the architecture boundary require updating the checker selections as well as
the guide. Keep version-specific external compatibility research in a dated
note with verified primary sources, leaving this core guide evergreen.
