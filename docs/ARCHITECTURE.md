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

## Philosophy and authority

The smallest useful unit is one task, one branch and one draft PR. A deterministic
check gives an exit-code verdict. An independent model reviews the authored head.
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
3. `pr` pushes the branch and creates or refreshes a draft PR. The local task
   supplies narrative; the live PR owns validation and review after creation.
4. `review` runs an independent reviewer against the exact authored SHA and
   publishes one GitHub review with findings. Review selection excludes every
   recognized contributing family from the configured-base merge-base through
   HEAD, including repairs and all trailers on each commit. Delta reviews and
   reposts enforce the same lineage-wide exclusion. Keep one authoring family
   per PR, including repairs; the other family may offer read-only advice or
   formal review. Imported and squashed work must retain actual child attribution,
   which cannot be inferred from the controller. Omitted trailers leave
   attribution unknown. There is one primary and at most one delta review per lineage; every changed head needs renewed review.
   Repairs after the delta require a class-wide audit and a fresh lineage under
   the contract, not an unlimited review loop.
5. `ready` derives readiness from the unchanged head, review, blocking findings,
   target branch and required CI. Its bounded waiter observes that same head.
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
                → independent model runner
                → GitHub PR, reviews, findings and required checks
                      → trusted hosted eligibility
                      → consumer CI and optional merge queue
                            → verified landed commit
```

The [sandbox](../src/loopzero/sandbox.py) bounds check writes, credentials,
network access and resources. [Reviewer runners](../src/loopzero/runners.py)
bound source access while allowing the network required by model CLIs; that
network means the sandbox cannot prevent exfiltration of readable data.
[GitHub transport](../src/loopzero/github.py) owns API access and changed-only
PR writes. [Hosted dispatch](../src/loopzero/hosted.py) and
[eligibility](../src/loopzero/eligibility.py) evaluate trusted review evidence.
[Mergify](MERGIFY.md) describes optional source-preserving queue integration;
[candidate validation](../src/loopzero/candidate.py) belongs at that boundary.

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
and review. Early owning tests, truthful model attribution, independent review,
changed-only writes and explicit ownership remain the design constraints.
The approved execution direction is T3 V2 for coordination, with loop-zero
retaining checks, formal independent review, readiness and verified merge
authority. This boundary is durable; verify runtime compatibility and deployment
status when adopting or updating an external executor.

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
