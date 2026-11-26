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
by default, with a fresh same-family fallback and visible disclosure when
independent runners are unavailable. The [bounded owner exception](../core/CONTRACT.md#owner-approved-contributor-review-exception)
permits disclosed contributor review with reduced assurance.
Readiness combines that evidence with live checks and findings. Merge verifies
what actually landed. CLI checks stay strict, with no waiver file or unsandboxed
fallback. The contract's owner queue route uses GitHub and the queue for
local-receipt exceptions. Merge authority permits merging by default outside
reserved classes.

Git owns commits, branches and ancestry. GitHub owns the live PR narrative,
reviews, finding threads, check statuses and merge state. Within a task worktree,
PR body writers lock the state directory from the live read through the update.
Review publication rechecks the round while holding a same-host lock keyed by
repository and PR. The lock covers POST and lost-response read-back.
CI and branch protection
provide the hosted enforcement. Readiness is a checklist over these sources,
not a tamper-proof security boundary: repository writers can edit PR evidence,
and branch protection remains essential.

Local `.loopzero/task.md` is working handoff context, rendered into the PR.
The CLI also writes a head-bound checks receipt and saved review output in
`.loopzero/` for local readiness and reposting. The CLI rejects tracked state
and symlinks and uses the worktree's state directory descriptor for receipt I/O.
GitHub retains authoritative delivery history. The retention rule for Codex
native audit receipts is described below. Local readiness requires
its current successful check receipt; hosted eligibility evaluates GitHub
review evidence using trusted base configuration. Local readiness and merge fetch
the configured origin base before loading its publisher policy. Source-check waiters refresh
the trusted policy on each poll. A base advance requires a new receipt only
when it changes the effective check settings.
Losing local artifacts may require rerunning checks or review, and never creates
a hosted approval.
Saved runner artifacts also retain the repository, PR and actual request publisher
before publication. A lost POST reply triggers a read-only reconciliation bounded
by one 30-second transport deadline, with no read retries. Recovery regenerates
the exact review body and finding locations, checks the complete review/comment
pages and unchanged head, and rereads review evidence before returning the saved
verdict. Pending, dismissed, conflicting or changing evidence blocks recovery.
`review --repost` reconciles before enforcing the remaining publication budget;
only an explicit retry after complete absence may publish again. The existing
self-review rejection fallback to COMMENT remains a separate transport rule.

Private review receipts, sidecars and malformed output excerpts live under the task worktree's `.loopzero/` and disappear when guarded merge cleanup removes the worktree. GitHub retains the published review evidence.

Private agent transcripts and metrics ledgers do not belong in this guide or in
a new persistent store.

## Delivery and feedback

The [console entry point](../src/loopzero/_cli_entry.py) parses arguments before
loading the delivery [commands](../src/loopzero/cli.py), which implement the contract's
[six steps](../core/CONTRACT.md#the-six-steps):

1. `start` creates an isolated worktree and task branch from the configured base.
   A retry can finish an interrupted initialization if the registered task
   worktree is clean, still at the remote base, and missing its task file.
2. Implementation proves the owning behavior at the narrowest public seam;
   focused tests start early. `check` runs deterministic checks in a read-only
   source sandbox. It inventories Git object stores only when alternates may be
   present. Its simple-command preflight catches missing local scripts,
   prepares recognized uv dependency groups offline in that sandbox and prints
   progress before each command, a metadata-only heartbeat after 30 seconds and every
   five minutes thereafter, and exit code/duration immediately after each stage.
   Heartbeats stop on completion or cancellation and never supply a verdict.
   Local stages drain merged child output and retain a bounded diagnostic tail.
   Trusted-base commands remain authoritative;
   stale task trees need integration only when prerequisites are missing,
   preserving source-preserving queue eligibility for behind branches.
   [Setup](../SETUP.md) defines the recognized command forms and cache remedy.
3. Branch PR lookup starts with the remote head and GitHub's commit-to-PR
   endpoint. It matches the source branch and repository, then re-reads PR
   detail. Lookup falls back to listing open PRs if the branch or matching open
   PR is absent, then checks the newest closed PR when requested. Check refresh
   is open-only. Before pushing
   or writing, `pr` requires a confirmed merge or a complete closed-history read
   through GitHub REST. A confirmed merge completes that branch even when a
   newer duplicate exists. If the head commit cannot prove completion, the
   pull-list fallback also checks earlier commits of the branch. Failed,
   malformed, repeated or truncated history blocks publication, with a limit of
   100 pages. An unfinished branch creates or refreshes a draft PR. Publication
   checks that every effective origin push URL targets the configured GitHub
   repository. A leased rewrite also requires the PR head in the task branch's
   local history.
4. `review` publishes one native review of the exact authored SHA. The contract's
   [review budget](../core/CONTRACT.md#review-budget) owns contributor exclusion,
   the PR-wide review budget across head rewrites, and the
   [owner-approved contributor-review exception](../core/CONTRACT.md#owner-approved-contributor-review-exception),
   which changes neither the selector nor the budget.
   The delta requires the reviewed commit on the current head's first-parent
   history and verifies merged parents against the live configured base. The
   runner receives topic patches in first-parent order and the difference
   between each automatic remerge and its recorded tree. Isolated Git verifies
   the history and builds the patch, excluding unrelated base changes.
   After an approved ordinary delta, one extra delta can cover a first-parent
   suffix consisting only of verified base merges with a conflict. Isolated Git
   reconstructs the automatic merges to confirm the conflict. Unresolved
   blocking findings or a third recorded review rule out this allowance.
5. `ready` derives readiness from the current head, its check receipt, an eligible review at that
   head or an eligible review covering an ancestor, blocking findings
   (unresolved threads a review publisher opened with a head-bound marker line),
   target branch and required CI. Readiness compares parsed instants for verdict
   freshness and uses a valid GitHub response Date during waits. The receipt must record a clean passing run
   at that head with the current commands and check settings digest. Ready and
   merge validate the PR, head and hold before checking the local receipt.
   An invalid receipt stops the command before it fetches remote readiness
   evidence. Writes to advance readiness or merging recheck the receipt.
   Status continues to collect all readiness diagnostics. A settings
   change or an older receipt without the digest requires a fresh check. The
   digest covers execution and stage reuse settings without recording raw
   environment values. For local sandbox receipts it omits inherited `PATH`,
   `LANG`, `LC_ALL`, `TERM`, and `HEAVY_SLOT_GRANTED_CORES` so readiness can
   run in a different host environment. Explicit settings, delegated runner
   environments, and stage reuse keys still bind these values. A receipt for
   delegated or input-keyed checks binds the configured base branch. Only the
   stage reuse key binds the exact base revision. Source-check waiters check for policy
   changes on every poll.
   Local readiness and hosted eligibility carry a review forward only across
   a first-parent chain of verified merges from the configured PR base. They
   reconstruct each merge in an isolated Git repository and compare its tree
   with the actual merge tree. Unavailable history blocks inherited coverage.
   Ancestor coverage includes outdated unresolved blocking threads. A primary
   that requested changes also requires resolved findings at the reviewed head.
   After its own draft-to-ready write,
   `ready --wait` revalidates through the same waiter and original deadline.
   Each poll resolves the trusted workflow ref, reusing parsed policy when the
   branch and immutable revision match the preceding observation.
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
procedures. CLI readiness needs local check success; that does not prove the integration
tree that a consumer's merge queue will land. Consumer CI must test that tree
when integration can change behavior. Nightly breadth supplies additional
feedback and needs an owner who responds to failures.

## Components and boundaries

```text
person / agent session / T3 workspace
  → loopzero CLI → Git worktree and authored commits
                → sandboxed owning checks
                → native model runner (cross-family preferred; disclosed same-family fallback)
                → GitHub PR, reviews, findings and required checks
                      → trusted hosted eligibility
                      → consumer CI and optional merge queue
                            → verified landed commit
```

The [sandbox](../src/loopzero/sandbox.py) bounds check writes, credentials,
network access and resources. Checks and their Bubblewrap probes pass
sandbox environment settings over a private file descriptor, keeping
forwarded values out of process arguments. [Reviewer runners](../src/loopzero/runners.py)
bound source access while allowing the network required by model CLIs; that
network means the sandbox cannot prevent exfiltration of readable data.
For a Codex executable installed inside protected native state, the implicit
runtime mount exposes only the resolved executable and its existing sibling
`codex-code-mode-host`, read-only. Neighboring history and configuration remain
hidden; helper aliases into protected state and explicit state mounts fail closed.
Check, pr, review, ready, merge, and status use the workflow at one remote-tracking
delivery revision selected by the workflow at Git's local `origin/HEAD`.
Missing trusted refs or workflows block these commands. An explicit
`--config` supplies validated policy for bootstrap or operator overrides.
Runners validate the complete mount plan against private review state and
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
that header, a newline and the Git patch, including carriage returns and the
final newline. UTF-8 patches are supplied verbatim. For other patches, the
prompt escapes invalid bytes and literal backslashes so the changes stay
visible. The native inspection digest always binds the original Git bytes. Git forces
text hunks for every file (overriding `-diff`, `binary` and NUL detection) and
runs no external diff command or text conversion, so changed lines stay visible.
Chunked review summarizes only files whose Git header reports a deletion.
The runner probes the selected Codex binary with `--version` and records its
stdout in runner provenance and the saved review receipt. A failed probe uses
RunnerBadOutput and existing family fallback before model input. Successful
probe output is accepted across CLI releases. Unsupported Claude options follow
existing native failure handling.
Native defaults and read-only tools remain enabled. Neither reviewer loads
instructions from the reviewed head, which the PR controls: Claude runs with
`--setting-sources ""`, and Codex runs with `project_doc_max_bytes=0` (no
AGENTS.md or AGENTS.override.md) and a `skills.config` entry disabling every
skill under the checkout's `.agents/skills` and `.codex/skills`; a skill
directory linking outside the checkout, or more skills than one argument can
disable, fails the attempt before launch. Repository
conventions reach reviewers only as evidence or through
`delivery.reviewer_ro_paths` from the pinned base. Structural transport tests do
not prove injection immunity, reviewer quality or backend identity.
The [dated design note](design/2026-10-07-deterministic-delivery-design-assumptions.md#r3-native-comparison--october-7-2026)
owns the implementation history, native comparison results and their limits.

Reviewer settings distinguish requested values, explicit launch configuration and
optional native reporting in the existing provenance container.
Adapters retain per-call requests; publication derives CLI claims from each retained
native envelope, ignoring unqualified compatibility fields and optional reported
metadata. Adapters record configured values only for model/effort flags they
explicitly pass at launch; missing flags stay unknown. Claude stores this record in
provenance while its raw output stays unchanged; normalized Codex evidence retains only
bounded recognized model/effort claims. Stderr banners take precedence; without a
banner, the adapter reads the exact JSON thread's native session rollout before
private HOME cleanup. It requires one matching rollout, matching session metadata,
a nonempty CLI version claim and consistent complete turn-context settings. Missing,
malformed, ambiguous or mismatched evidence remains unknown. Codex session
persistence is enabled only in the existing temporary private HOME, which is
removed after capture. Rollout reads stay inside that HOME and reuse the owned,
single-link regular-file snapshot reader, opening without following symlinks or
blocking on special files and refusing files over 8 MiB before reading. Reviewers
can write these session files, so their claims retain the existing trusted
publisher boundary and do not prevent coherent forgery or attest the backend.
Per-call configured values remain separately
labelled in human output and optional Codex projection fields; they never become
CLI-reported claims. Missing native recovery settings may use these launch records,
but conflicting reported values still HOLD. Legacy requested/model/effort metadata
cannot backfill a missing launch record.
Chunk merge pads missing request entries and preserves their native-call order.
Legacy model/effort fields retain their historical adapter meanings for loading;
qualified summaries never use them as execution evidence. Eligibility still checks
core native provenance. Exact read-back tries legacy summaries when requested settings
are absent and the pre-#344 Claude requested/reported layout when requested settings
exist without configured settings. It still verifies exact published evidence.
New publication and reposting use the current layout.

<a id="codex-review-runner-evidence"></a>

A structured Codex event reporting that Code Mode cannot spawn its host
invalidates the attempt even if the process exits zero with a parseable verdict.
The runner excludes the attempt from chunk merge, publication and saved-result
replay without consuming review budget. Quoted diagnostics and absence of tool
calls alone do not invalidate a review.
Eligibility rejects fatal latest evidence without reviving an older
approval. The retired [incident adjudication](design/2026-10-27-codex-startup-adjudication.md)
remains historical evidence. The leading-block reader still validates archived
adjudication markers alongside their original evidence.
New Codex publication derives one canonical v1 `codex-evidence` comment from the
complete validated private native stream, across every ordered chunk/session.
It immediately follows the byte-zero review marker, before human text. The anchored leading block is limited to 8192 bytes; strict schema,
canonical ASCII base64url JSON, context and bounds checks reject malformed or
ambiguous projections without legacy fallback. Duplicate blocks fail eligibility,
and the latest invalid evidence cannot revive an older approval. Header overflow
produces HOLD without truncation. The projection binds publisher,
repository/PR, full head/kind, the existing relative receipt reference, exact UTF-8
raw digest/byte count, completeness and per-call requested/reported/configured settings,
fatal classification and attributable inspection event/range facts. It publishes
no commands, source contents or private paths. New reviews show structured
review fields and a local retention notice for both Codex and Claude.
Historical transcript rendering is available only for exact read-back matching.
Local publication, saved-result replay and exact confirmation regenerate these
facts from complete raw, never the public transcript tail or a saved summary.
The restricted receipt stays in the task worktree until completion.
The review command locks the worktree’s Git directory from round selection
through publication to reserve the round across overlapping local invocations.

After confirming a merge, cleanup reads the remote branch ref and deletes it
only when its SHA matches the merged PR head. An absent ref completes that
step, and a moved ref is left in place with a warning. The Git push lease
checks the expected SHA at deletion time even if the ref changes after the REST read.
The push uses a temporary bare repository and ignores local, global, and system
Git configuration so URL rewrites cannot change its destination.
After confirmed remote deletion or absence, cleanup deletes the matching
`origin/lz/<slug>` tracking ref only if it still points to the merged head.
An already-merged PR can resume cleanup from its task worktree. Local removal
requires a clean worktree with no ignored user files. A Git ref lock keeps the
worktree at the merged PR head during removal. Cleanup then deletes the branch
only if it still points at that head.
Hosted eligibility trusts the configured publisher's projection and live identity,
state, head and latest ordering. It cannot recompute private raw or defeat a
coherent forgery by a trusted publisher/editor; native claims do not attest the
backend. Ordinary legacy/truncated/tool-free evidence remains UNKNOWN under its
existing contract. New named recoveries require complete attributable evidence
and HOLD when required commissioned evidence is missing or unknown.
Separate successful native head and immutable-range diff commands in one session
can qualify when explicit repository anchors, event outcomes and exact output
match the pinned head/diff. Echoes, ambiguous shell, wrong repositories, unresolved
refs, failed commands and cross-session combinations cannot supply that proof.
Native runner failures distinguish subscription exhaustion from transient throttling:
weekly/session-limit text and Claude's blocking-limit terminal reason identify quota;
HTTP 429 alone does not. This classification neither switches accounts nor retries
review publication; the existing reviewer selector and delivery gates still apply.
[GitHub transport](../src/loopzero/github.py) owns API access and changed-only
PR writes. [Hosted dispatch](../src/loopzero/hosted.py) and
[eligibility](../src/loopzero/eligibility.py) evaluate trusted review evidence.
Local readiness and hosted source and candidate eligibility reject PRs with
merge-authority holds. Adding or removing a hold refreshes source eligibility
even when the head stays the same. Merge admission rechecks the live hold.
Hosted evaluation uses the source snapshot already fetched by dispatch or
candidate attestation. The caller rechecks the source fields that affect the
verdict before publication. Standalone evaluation performs its own PR reads.
[Mergify](MERGIFY.md) describes optional source-preserving queue integration;
[candidate validation](../src/loopzero/candidate.py) belongs at that boundary.
Hosted attestation uses the trusted workflow's base branch for candidate and
source targets, queue status and integration tree verification.
When delivery uses the existing size checker, local readiness and new Mergify
requests evaluate a temporary integration with the freshly fetched target. The
target supplies the checker and baseline. Existing requests continue to the
queue wait path. Readiness and new queue requests without an admission deadline can project an
advisory budget after a single known chain of queued predecessors.
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
GitHub reads and the idempotent thread resolution mutation retry transient
failures once after five seconds. They stop on exhausted quota and honor a
secondary limit’s Retry-After when the delay fits the read budget. Mergify
reads retry server errors, throttling and failed
reads up to twice (2/5 seconds). Waits use bounded backoff. See the
[contract's waiter and polling rules](../core/CONTRACT.md#failure).
The optional host ai-accounts helper owns managed credential selection and exact-model
eligibility under its existing lock. Its Claude exec reserve guard (including pinned
identities) reads the same per-window usage as status and refuses non-owner launches
at 85% of any reported limit, including a scoped weekly sub-cap, or with
missing/unreadable account status or status older than 20 minutes. The owner environment
override permits required work; direct T3, run and shims are outside this guard.
Claude usage prefers the read-only OAuth usage API; setup tokens without profile
access fall back to the existing model probes, explicitly labelled with the API
failure. Claude credentials retain their existing stored-bearer-token format;
native login/refresh integration is outside this usage-reporting helper.
No login, account activation or T3 provider configuration is performed by reporting.
Each status window exposes `instance`, `window`, `used_pct`, `resets_at` (ISO UTC),
`resets_local`, `snapshot_age_s` and `unknown_reason`, alongside source and fetch
time. Text and JSON derive from these same windows; user-facing dates use explicit
Europe/Warsaw zoneinfo with CET/CEST labels, regardless of the host timezone.
Codex uses its read-only usage API and classifies windows by their duration, since
the primary window can itself be weekly. Output keeps every reset with its window,
shows Fable before the overall Claude week, and only shows a Claude session warning
when that window is reported. Per-window resets include healthy windows; legacy
reset fields represent when all binding windows can clear, unknown if any binding
reset is missing. Pace estimates use consecutive observations of the same window,
source and reset; a refill or reset change invalidates the estimate. They are local
linear projections, not provider forecasts. The existing status file holds this
information; no additional usage state or policy gate is introduced.
Nonsecret launch identities pin credential bindings
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

The existing repository coordinator or T3 integration maintainer also follows
[T3 upstream review](T3-UPSTREAM.md): weekly reading during active use and review
before relevant upgrades or compatibility repairs. This is maintenance triage,
not a new scheduler or permission to adopt upstream policy. Version-specific
comparisons remain dated evidence; the running tools and instructions must be
reconciled with matching source before changing the integration.

Run `uv run --python 3.14 python scripts/check_architecture_docs.py [BASE]`. It compares the base
(default: merge-base of HEAD and `origin/main`) with the working tree, including
new files, and validates local Markdown links and anchors in this guide, the docs
index, README and AGENTS guidance without network access. The repository's
`workflow.toml` runs the default gate (alongside `tach check` against
[tach.toml](../tach.toml) for module dependency boundaries); PR and merge-group
CI enforce it against the event's base SHA. Push-to-main CI runs `--links-only`, because squash merging
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

## Check stage runners

Local stages run in Bash POSIX mode with pipefail enabled. A pipeline returns
the status of its rightmost failed member, or zero when all members succeed.
The local shell is part of the receipt and stage reuse fingerprints.

`[checks].runner` is an operator-trusted argv prefix executed outside local bwrap,
from a fresh temporary directory outside the task worktree. Delegation does not
require local bwrap. Its executable must
be an absolute path or a bare PATH command; later arguments may be relative.
Execute the configured path (or PATH lookup result) without resolving symlinks.
For runner containment, resolve the executable, absolute arguments, joined short
option paths, `--opt=/path` values and colon-separated path lists: none may
reference task-tree files, including through symlinks. Relative parent traversal
must remain inside the fresh runner directory. The runner program and its argv
must remain operator-controlled and must not come from the task tree.

Each stdin JSON object contains `worktree` (resolved absolute checkout path),
`head` (exact SHA), `base` (resolved base SHA),
`command` (one locally resolved stage), `env` (`SANDBOX_ENV` overlaid with
configured checks env, excluding host allowlist values), `network` (boolean),
and `env_sha256`. The digest is SHA-256 of UTF-8 JSON of `env`, with sorted keys,
ASCII escaping and compact separators (`,` and `:`). Stdout must contain exactly
one JSON object echoing worktree, head, base, command, network and env_sha256, plus
`exit_code` (integer), `duration_s` (finite nonnegative number), `not_started`
(boolean), and `tail` (string). Env values must not be echoed. Diagnostics belong
on stderr. Missing, extra, duplicate, malformed or mismatched fields and runner
process failures fail the stage closed. Runner timeouts report exit code 124
and the measured elapsed duration. `not_started=true` requires exit_code=0,
duration_s=0 and empty tail, and permits only local fallback through bwrap.

The runner may read `worktree` as data to execute the exact clean head, including
unpushed local commits, with the supplied environment and network policy under
equivalent isolation, and must not claim not_started after launch. The coordinator resolves stages, verifies clean unchanged head before
every stage and after execution, aggregates in order and stops on failure. No
stage is retried after success or uncertain launch. Only the coordinator writes
the authoritative `checks.json` receipt. Writers lock the task state directory
while rechecking HEAD and atomically replacing the receipt from a synced sibling
temporary file. Successful keyed stages are checkpointed in the same receipt
for reuse. A checkpoint cannot replace a clean pass for the same head and
settings or satisfy readiness before final worktree validation. Completed
failures still replace passes. Runners cannot select stages or mint receipts. Stage admission
belongs to the runner: the outer `loopzero check`
must not hold a heavy slot when a runner is configured. Without a runner, the
existing local execution path is unchanged.

Declared `[checks.inputs]` pathspecs opt commands into local passing-result reuse.
Keys bind committed paths/blob IDs, the bytes of the checked-out regular files,
command, version, sandbox configuration and the resolved base SHA. A stage runs
fresh when a matched path cannot be identified as a readable regular file.
A base advance prevents reuse in a fresh check. Readiness can still accept
the existing passing receipt at the same task head if its settings match.
With explicit `--config`, reuse can fall back to the recorded task base if the
remote ref is absent. Without either base, local stages run fresh.
The `"*"` entry adds shared inputs to listed commands. Unlisted commands reuse only
when `"*"` includes `"."` (the whole tree); a partial or empty `"*"` leaves them unkeyed.
Consumers own complete input declarations.
Reuse only successful keyed stages from this worktree's clean receipt or
checkpoint. Dirty trees run fresh. A tracked receipt is never eligible for reuse.
Receipts that use saved stage results bind the current head and check settings.
For delegated checks, both the receipt and the stage key bind the runner's launch
path, resolved executable path and executable bytes. Unavailable runner identity
prevents receipt reuse.

A worker running the entire check is itself the coordinator and writes a
receipt with its own effective settings. Transferring it preserves the
fingerprint, if present. Local readiness applies the same receipt predicate
as for any other coordinator. See [check receipts with remote execution](../SETUP.md#check-receipts-with-remote-execution)
for the operator route.

CI and merge-queue candidates run fresh without a local worktree receipt.
