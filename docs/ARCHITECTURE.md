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

The [CLI](../src/loopzero/cli.py) implements the contract's
[six steps](../core/CONTRACT.md#the-six-steps):

1. `start` creates an isolated worktree and task branch from the configured base.
2. Implementation proves the owning behavior at the narrowest public seam;
   focused tests start early. `check` runs deterministic checks in a read-only
   source sandbox. Its simple-command preflight catches missing local scripts,
   prepares recognized uv dependency groups offline in that sandbox and prints
   progress before each command. Trusted-base commands remain authoritative;
   stale task trees need integration only when prerequisites are missing,
   preserving source-preserving queue eligibility for behind branches.
   [Setup](../SETUP.md) defines the recognized command forms and cache remedy.
3. Before pushing or writing, `pr` reads the branch's closed PR history through
   GitHub REST. A confirmed merged PR completes that branch, even when a newer
   duplicate exists; the caller must start a new task. Absence requires complete
   typed pages, bounded to 100; failed, malformed, repeated or truncated history
   blocks publication. Other commands use open-first, newest-closed lookup. An
   unfinished branch creates or refreshes a draft PR.
4. `review` publishes one native review of the exact authored SHA. The contract's
   [review budget](../core/CONTRACT.md#review-budget) owns contributor exclusion,
   primary/delta limits and the
   [owner-approved contributor-review exception](../core/CONTRACT.md#owner-approved-contributor-review-exception),
   which changes neither the selector nor the budget.
5. `ready` derives readiness from the unchanged head, review, blocking findings,
   target branch and required CI. After its own draft-to-ready write,
   `ready --wait` revalidates through the same waiter and original deadline.
   This is best-effort observation: an immediate green read may precede
   asynchronous CI scheduling, there is no unconditional delay, and merge
   admission checks readiness again. Missing-run diagnosis keeps its two-minute
   grace after activation without extending the deadline.
   When every non-success signal of a required check strictly predates the eligible
   review, `ready --wait` allows up to 15 minutes from that review for a refresh,
   including time before GitHub publishes pending. Past the ordinary two-minute
   review grace it identifies the old verdict and awaited refresh. The check still
   blocks readiness throughout; at 15 minutes it becomes a final failure again
   unless existing pending/replacement evidence independently permits waiting.
   Failures at or after the review receive no extended grace. The command's own
   timeout can end waiting sooner.
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
explicit exceptions. Claude's environment routes are `ANTHROPIC_API_KEY` and
`CLAUDE_CODE_OAUTH_TOKEN`: a nonempty value (whitespace included) is forwarded
unchanged, omits host config/global-state binds and uses disposable private state;
the runner never adopts disk auth after an environment error, and native Claude
owns route precedence and billing. Empty or absent values retain the file-backed
binds of `~/.claude` (or `CLAUDE_CONFIG_DIR`) and `~/.claude.json`, with their broad
config and mixed global-state exposure. Codex keeps its `auth.json` bind under
`CODEX_HOME` or `~/.codex`, including when `OPENAI_API_KEY` is forwarded.
File-backed binds preserve in-place writes, without guaranteeing host replacement
visibility or shared-lock coordination. This boundary is route-specific, not full
filesystem privacy. An implicit runtime inside state receives only the selected
executable's exact read-only file mount, while explicit runtime paths have no
exception. This preserves standalone CLI startup without exposing neighboring
state; bundles needing protected siblings require installation outside state.
The contract states the agent-facing consequences.

The R3 runner separates fixed reviewer policy from supplied material
inside the existing runner adapters. A fixed ASCII paragraph and validated scope
enter Claude's append-system-prompt or Codex's developer_instructions override;
commit identity stays in the JSON material header. Explicit UTF-8 stdin carries
that header, a newline and the exact raw diff without trimming task or diff text.
The selected Codex binary's version probe accepts only source-reviewed 0.160.1,
0.161.0, 0.162.1, 0.163.0 and 0.164.0; refusal uses RunnerBadOutput and existing family fallback
before model input. Unsupported Claude options follow existing native failure handling.
Native defaults, repository AGENTS.md/CLAUDE.md processing and read-only tools
remain enabled. Repository instructions remain a native authority surface, so
structural transport tests do not prove injection immunity, reviewer quality or
backend identity.
The [dated design note](design/2026-10-07-deterministic-delivery-design-assumptions.md#r3-native-comparison--october-7-2026)
owns the implementation history, native comparison results and their limits.

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
Native runner failures distinguish subscription exhaustion from transient throttling:
weekly/session-limit text and Claude's blocking-limit terminal reason identify quota;
HTTP 429 alone does not. This classification neither switches accounts nor retries
review publication; the existing reviewer selector and delivery gates still apply.
[GitHub transport](../src/loopzero/github.py) owns API access and changed-only
PR writes. [Hosted dispatch](../src/loopzero/hosted.py) and
[eligibility](../src/loopzero/eligibility.py) evaluate trusted review evidence.
[Mergify](MERGIFY.md) describes optional source-preserving queue integration;
[candidate validation](../src/loopzero/candidate.py) belongs at that boundary.
Candidate lineage reads wait on zero or multiple live batch matches with exponential
backoff capped at 30 seconds and a shared 90-second retry-admission deadline across
both reads and all attestation attempts. Each wait revalidates the candidate before
and after sleeping; closure is a terminal superseded-candidate error. Malformed
status and other identity failures are not retried, and success still requires
exactly one batch and stable lineage. Caller-owned synchronous reads are not preempted. Batch sleep time does not
consume the separate race-retry window, but race retries remain capped by the
shared 90-second deadline. A second-read wait lengthens the existing interval
between source policy evaluation and final snapshots; policy evidence is not atomic.
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
GitHub reads retry transient failures up to three times (5/20/60 seconds) and
stop on rate limits; Mergify reads retry server errors, throttling and failed
reads up to twice (2/5 seconds). Waits use bounded backoff and one waiter per PR;
direct `gh` polling adds shared account load and cannot replace the wait command.
A rate limit is a waiting dependency, not permission to turn missing evidence green.
The optional host ai-accounts helper owns managed credential selection and exact-model
eligibility under its existing lock. Nonsecret launch identities pin credential bindings
(Codex account principals survive routine OAuth refreshes);
explicit caller credentials and billing routes remain unchanged. Harnesses own safe live
session replacement, including effects and native history. The shim does not replay
whole `loopzero` commands. An explicitly repeatable one-shot helper caller may opt into
one retry after its effective credential changes; this supplies no delivery evidence or
permission to replay uncertain tool actions or review publication.

## Direction and historical evidence

The [September 24 architecture note](design/2026-09-24-delivery-system-architecture.md)
is dated consumer evidence and its accepted principles. Its measurements, gap
table and owners describe that window, not the current backlog; verify a gap
against its owning code and consumer before calling it open or closed.

The October 5 direction keeps this guide as the current entry point, preserves
the small delivery toolkit, and makes architecture upkeep part of contribution
and review. Early owning tests, truthful model attribution, independent review
by default with the bounded owner exception, changed-only writes and explicit
ownership remain the design constraints.
The approved execution direction is T3 V2 for coordination, with loop-zero
retaining checks, formal native review under that policy, readiness and verified
merge authority. This boundary is durable; verify runtime compatibility and
deployment status when adopting or updating an external executor.

The [October 5 T3 verification snapshot](design/2026-10-07-deterministic-delivery-design-assumptions.md#t3-verification-snapshot--october-5-2026)
records the runtime evidence behind this boundary: at-least-once notifications,
watcher limits and consumer migration constraints. A new daemon, scheduler,
reminder issue or metrics ledger would add state without improving the current
authority boundary and is outside this direction.

## Keeping the guide current

Architecture review is event-driven. When changing commands, configuration,
state authority, sandbox/trust boundaries, review/readiness, queue integration,
module ownership or delivery routines, read this guide and the owning sources
before finishing. Update the affected explanation and links in the same PR, or
explain why the guide remains accurate. A reviewer checks that claim against the
diff, challenges misplaced ownership and distinguishes shipped behavior from
proposals and dated observations.

Run `python3 scripts/check_architecture_docs.py [BASE]`. It compares the base
(default: merge-base of HEAD and `origin/main`) with the working tree, including
new files, and validates local Markdown links and anchors in this guide, the docs
index, README and AGENTS guidance without network access. The repository's
`workflow.toml` runs the default gate; PR and merge-group CI enforce it against
the event's base SHA. Push-to-main CI runs `--links-only`, because squash merging
can drop authored trailers and landed history cannot re-establish the pre-merge
acknowledgement; that mode still rejects broken local links and anchors.
`loopzero check` pins checks to trusted base configuration, so run a new or
changed gate directly until its configuration lands on the base.

The checker watches source-owned contracts, skill routing, the example and actual
workflow, hosted workflow files, itself, and selected CLI/configuration interfaces;
it lists the exact paths and AST selections and ignores formatting and
implementation-only edits outside them. A watched change requires a guide edit
in the same comparison, or a commit trailer in the changed range:

```text
Architecture-Review: unchanged; <concrete reason the guide is still accurate>
```

The reason must be nonblank and at least 20 characters, and real: for example, a
workflow dependency pin that leaves triggers, required checks and trust
boundaries unchanged. Git history holds the acknowledgement; there is no separate
review ledger. Uncommitted watched changes need a guide update or a committed
acknowledgement before the final check. A missing base, unreadable source,
malformed Python or broken local link fails the check. CI does not check external
websites.

Automation proves that review was recorded and links resolve. It cannot prove
semantic freshness, detect every architecture change, or establish that an
acknowledgement is true; contributors and reviewers own that, including changes
outside the watched surfaces. Changes to the architecture boundary require
updating the checker selections as well as the guide. Keep version-specific
external compatibility research in a dated note with verified primary sources,
leaving this guide evergreen.
