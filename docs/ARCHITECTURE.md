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
compatibility investigation. Its delivered changes are in code; this guide and the
delivery contract describe current behavior.

## Philosophy and authority

The smallest useful unit is one task, one branch and one draft PR. A deterministic
check gives an exit-code verdict. A model reviews the authored head, independently
by default, with a fresh same-family fallback and visible disclosure when
independent runners are unavailable.
Readiness combines that evidence with live checks and findings. Merge verifies
what actually landed. CLI checks stay strict, with no waiver file or unsandboxed
fallback. The contract's owner queue route uses GitHub and the queue for
local-receipt exceptions. Merge authority permits merging by default outside
reserved classes.

Git owns commits, branches and ancestry. GitHub owns the live PR narrative,
reviews, finding threads, check statuses and merge state. PR body writers hold
the same-host repository-and-PR lock from the live read through the update.
Review publication holds that lock for its round recheck, POST, read-back, and
body summary. Draft activation, Mergify request and confirmation comments, and
finding replies hold the same PR lock across their live state checks and writes.
A finding retry checks for the caller's matching reply on resolved threads
before acting on an open thread with the same finding ID.
Mergify admission takes the worktree-local lock first to protect receipt
validation, and queue polling holds neither lock. Check coalescing and receipts
retain the worktree-local lock.
CI and branch protection
provide the hosted enforcement. Readiness is a checklist over these sources,
not a tamper-proof security boundary: repository writers can edit PR evidence,
and branch protection remains essential.

Local `.loopzero/task.md` is working handoff context, rendered into the PR.
The CLI also writes a head-bound checks receipt and saved review output in
`.loopzero/` for local readiness and reposting. The CLI rejects tracked state
and symlinks and uses the worktree's state directory descriptor for receipt I/O.
GitHub retains authoritative delivery history. Local readiness requires
its current successful check receipt; hosted eligibility evaluates GitHub
review evidence using trusted base configuration. Local readiness and merge fetch
the configured origin base before loading its publisher policy. Source-check waiters refresh
the trusted policy on each poll. A base advance requires a new receipt only
when it changes the effective check settings.
Losing local artifacts may require rerunning checks or review, and never creates
a hosted approval.
Saved runner artifacts also retain the repository, PR and actual request publisher
before publication. A lost POST reply triggers a read-only reconciliation bounded
by one 30-second transport deadline. Recovery regenerates
the exact review body and finding locations, checks the complete review/comment
pages and unchanged head, and rereads review evidence before returning the saved
verdict. Pending, dismissed, conflicting or changing evidence blocks recovery.
When no diff line can anchor a blocker, the review body carries it behind a
leading marker older readers reject. It blocks readiness until an eligible
review covers the fixes.
`review --repost` reconciles before enforcing the remaining publication budget;
only an explicit retry after complete absence may publish again. The existing
self-review rejection fallback to COMMENT remains a separate transport rule.

Tests use disposable user and reviewer homes. Credential refresh tests bind
only the synthetic files they create. The generic fake reviewer has no
credential read or write behavior.

Private review receipts and malformed output excerpts live under the task worktree's `.loopzero/` and disappear when guarded merge cleanup removes the worktree. GitHub retains the published review evidence.
Outside the worktree the CLI keeps per-PR write locks in `~/.cache/loopzero/pr-locks`,
runs the optional GitHub App token helper `~/.config/loopzero/app-token` (the
identity that publishes trusted reviews) and reads the Mergify key from
`~/.config/mergify/api-key`; setup describes the
[token helper](../SETUP.md#hosted-review-eligibility) and [key](../SETUP.md#mergify).
The optional token helper assumes a single-user delivery host. Its executable
and full path, including resolved symlink paths, must be protected from writes
by other unprivileged local users.

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
   Local stages drain merged child output into a bounded capture window. They
   redact supported credential shapes and forwarded secrets before recording or
   displaying the tail, including on timeout. If the window starts inside an
   oversized record, the stage omits the uncertain prefix. Older check receipts
   require a fresh check because their tails may have been cut before redaction.
   Between stages, a locally observed trusted plan change stops the check
   before it runs more obsolete commands. This read makes no network request.
   Trusted-base commands remain authoritative;
   stale task trees need integration only when prerequisites are missing,
   preserving source-preserving queue eligibility for behind branches.
   [Setup](../SETUP.md) defines the recognized command forms and cache remedy.
3. Branch PR lookup uses GitHub's commit-to-PR endpoint. Review, ready, merge and check refresh start
   with the local HEAD and verify it still matches the PR. Other branch lookups
   resolve the remote head. Lookup matches the source branch and repository,
   then re-reads PR detail. It falls back to listing open PRs if the branch or a
   matching open PR is absent, then checks the newest closed PR when requested. Check refresh
   is open-only. Before pushing
   or writing, `pr` requires a confirmed merge or a complete closed-history read
   through GitHub REST. A confirmed merge into the configured base completes
   that branch even when a newer duplicate exists. If the head commit cannot
   prove completion, the fallback scans closed PRs by source branch across up
   to 100 REST pages.
   Invalid or incomplete history blocks publication. An unfinished branch
   creates or refreshes a draft PR. All effective origin push URLs must target
   the configured repository. A leased rewrite requires the PR head in the task branch's
   local history. The push source is the captured commit. A new draft starts with task prose.
   The body patch, including saved evidence on a new draft, requires an open PR at the
   captured commit and configured target.
4. `review` requires a matching check receipt before launching a native review
   of the exact authored SHA. A failed receipt requires green required hosted
   checks on that SHA, excluding the review eligibility status that depends on
   the review itself. The failed receipt must match a check prefix, preparation
   or preflight failure from the trusted plan.
   The owner queue route still governs queue admission.
   Reposting saved output can recover publication
   without launching a new review. The contract's
   [review budget](../core/CONTRACT.md#review-budget) owns contributor exclusion
   and the PR-wide review budget across head rewrites; truthful all-writer
   trailers decide the reviewer family.
   Immediately before posting, the live PR must be open at the reviewed head against the configured base; otherwise the saved review stays.
   The delta requires the reviewed commit on the current head's first-parent
   history and verifies merged parents against the live configured base. The
   runner receives topic patches in first-parent order and the difference
   between each automatic remerge and its recorded tree. Isolated Git verifies
   the history and builds the patch, excluding unrelated base changes.
   After an approved ordinary delta, one extra delta can cover a first-parent
   suffix consisting only of verified base merges with a conflict. Isolated Git
   reconstructs the automatic merges to confirm the conflict. Unresolved
   blocking findings or a third recorded review rule out this allowance.
   An empty base-merge delta cannot bypass unresolved blocking findings.
   Without review coverage, readiness also shows outdated unresolved findings.
5. `ready` derives readiness from the current head, its check receipt, an eligible review at that
   head or an eligible review covering an ancestor, blocking findings
   (unresolved threads a review publisher opened with a head-bound marker line),
   target branch and required CI. Readiness compares parsed instants for verdict
   freshness and uses a valid GitHub response Date during waits. The receipt must record a clean passing run
   at that head with the current commands and settings fingerprint. Ready and
   merge validate the PR, head and hold before checking the local receipt.
   An invalid receipt stops the command before it fetches remote readiness
   evidence. Writes to advance readiness or merging recheck the receipt.
   Status continues to collect all readiness diagnostics. A settings
   change or an older receipt without the fingerprint requires a fresh check. The
   fingerprint covers execution and stage reuse settings without recording raw
   environment values. For local sandbox receipts it omits inherited `PATH`,
   `LANG`, `LC_ALL`, `TERM`, and `HEAVY_SLOT_GRANTED_CORES` so readiness can
   run in a different host environment. The receipt also ignores whether the
   inherited CPU grant is allowlisted. Explicit settings and stage reuse keys
   still bind these values. Both keys disregard allowlist order, duplicate names,
   and unset names outside the default allowlist. Readiness can also match the
   prior raw allowlist hash against the current names, with or without the
   inherited CPU grant. A receipt for input-keyed checks binds the
   configured base branch. Passing keyed receipts also bind the declared file bytes.
   The saved stage key pairs a byte digest with its execution digest. Only stage reuse
   compares the execution digest, which binds the exact base revision. Older keyed
   receipts require a fresh check. Configured inputs must match readable, regular
   worktree files to yield a byte digest. A missing base revision still permits
   a byte digest and disables stage reuse. Source-check waiters check for policy
   changes on every poll.
   Local readiness and hosted eligibility share the mergeability verdict.
   Hosted source refresh retries an unknown result on the same head before
   leaving it pending.
   Local readiness and hosted eligibility carry a review forward only across
   a first-parent chain of verified merges from the configured PR base. They
   reconstruct each merge in an isolated Git repository and compare its tree
   with the actual merge tree. Unavailable history blocks inherited coverage.
   Ancestor coverage includes outdated unresolved blocking threads. A primary
   that requested changes also requires resolved findings at the reviewed head.
   Readiness keeps the highest run ID for each check name, GitHub App and
   producer (Actions workflow when identified, otherwise check suite).
   It combines verdicts by required name and keeps only the newest commit status
   per case-insensitive context.
   A positive `ready --wait`
   budget covers the first readiness read, the live PR reread before draft activation,
   and revalidation after taking a PR out of draft.
   The live PR read before activation blocks a moved head, changed target, closed state, hold, or merge conflict.
   The live REST detail supplies the node ID for the GraphQL activation mutation. Without a node ID, the CLI uses
   the GitHub CLI activation command. After a transient write failure within a positive wait budget, the waiter
   can resume if GitHub already applied the write. One backed-off retry requires known mergeability, fresh
   eligibility and a final PR read that still agrees with the checked snapshot.
   Waiting is best-effort observation: short grace periods cover CI scheduling
   and a required check refreshing after an eligible review (constants in
   [cli.py](../src/loopzero/cli.py)), the check blocks readiness throughout,
   the command's own timeout can end waiting sooner, and merge admission
   checks readiness again.
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
bound source access while allowing the network required by model CLIs; see the
contract's [sandbox rules](../core/CONTRACT.md#sandbox-rules-for-checks) for what that
network permits.
For a reviewer executable installed inside protected native state, the implicit
runtime mount exposes only the resolved executable, read-only; for Codex it also
exposes the existing sibling `codex-code-mode-host`, read-only. Neighboring history and configuration remain
hidden; helper aliases into protected state and explicit state mounts fail closed,
and explicit runtime paths get no such exception.
Every command except `start` uses the workflow at one remote-tracking
delivery revision selected by the workflow at Git's local `origin/HEAD`.
Trusted revision resolution and workflow reads disable Git object replacement.
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
filesystem privacy.
The contract states the agent-facing consequences.

Each reviewer runner separates fixed reviewer policy from supplied material
inside the existing runner adapters. A fixed ASCII paragraph and validated scope
enter Claude's append-system-prompt or Codex's developer_instructions override;
commit identity stays in the JSON material header. Explicit UTF-8 stdin carries
that header, a newline and the Git patch, including carriage returns and the
final newline. UTF-8 patches are supplied verbatim. For other patches, the
prompt escapes invalid bytes and literal backslashes so the changes stay
visible. The evidence diff digest always binds the original Git bytes. Git forces
text hunks for every file (overriding `-diff`, `binary` and NUL detection) and
runs no external diff command or text conversion, so changed lines stay visible.
Chunked review summarizes only files whose Git header reports a deletion.
Unsupported Claude options follow existing native failure handling.
Native defaults and read-only tools remain enabled. Neither reviewer loads
instructions from the reviewed head, which the PR controls: Claude runs with
`--setting-sources ""`, `--restricted` (file tools confined to the checkout, so
host files and auto-memory stay out) and `includeGitInstructions` off (no
PR-controlled branch names or commit subjects in its context), and Codex runs
with a private HOME, no MCP servers and `project_doc_max_bytes=0` (no
AGENTS.md or AGENTS.override.md) and a `skills.config` entry disabling every
skill under the checkout's `.agents/skills` and `.codex/skills`; a skill
directory linking outside the checkout, or more skills than one argument can
disable, fails the attempt before launch. Repository conventions reach
reviewers only as evidence; `delivery.reviewer_ro_paths` mounts reviewer CLI
runtime files only. Structural transport tests do
not prove injection immunity, reviewer quality or backend identity.
The [dated design note](design/2026-10-07-deterministic-delivery-design-assumptions.md#r3-native-comparison--october-7-2026)
owns the implementation history, native comparison results and their limits.

Reviewer provenance keeps requested, launch-configured and natively reported
model/effort settings apart. Only native evidence becomes a CLI-reported claim:
for Claude, `modelUsage` in each retained result envelope (effort stays
unknown); for Codex, its stderr banner or the matching session rollout captured
in the temporary private HOME; missing, malformed or conflicting evidence stays unknown or
holds. Reviewers can write that evidence, so it stays inside the trusted
publisher boundary below.

<a id="codex-review-runner-evidence"></a>

A structured Codex event reporting that Code Mode cannot spawn its host
invalidates the attempt even if the process exits zero with a parseable verdict.
The runner excludes the attempt from chunk merge, publication and saved-result
replay without consuming review budget. Quoted diagnostics and absence of tool
calls alone do not invalidate a review. The leading-block reader still validates archived
adjudication markers alongside their original evidence.
New Codex publication derives one canonical `codex-evidence` comment, placed
right after the review marker, from the complete validated private native
stream. It binds publisher, repository/PR, head and review kind, the receipt
reference, raw digest, completeness, per-call settings and fatal classification,
and publishes no commands, source contents or private paths. Malformed,
duplicate or oversized projections fail eligibility without truncation or legacy
fallback, and invalid or fatal latest evidence never revives an older approval.
Publication, replay and confirmation regenerate it from complete raw output; the
restricted receipt stays in the task worktree until completion.
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
An already-merged PR can resume cleanup from its task worktree. A merge
invocation that finds a closed, unmerged PR can remove its worktree and keeps
both branch refs. Local removal requires a linked checkout in the primary
repository's managed worktree directory with the marker written to its task
file by `start`. It allows ignored files in a clean worktree and refuses to
remove nested worktrees. Git removal stays non-forced. A Git ref lock keeps the
worktree at the final PR head during removal. After a merge, cleanup deletes
the local branch only if it still points at that head.
Hosted eligibility trusts the configured publisher's projection and live identity,
state, head and ordering. It cannot recompute private raw output or defeat a
coherent forgery by a trusted publisher, and native claims do not attest the
backend. Legacy, truncated or tool-free evidence stays unknown.
Native runner failures distinguish subscription exhaustion from transient throttling:
weekly/session-limit text and Claude's blocking-limit terminal reason identify quota;
HTTP 429 alone does not. This classification neither switches accounts nor retries
review publication; the existing reviewer selector and delivery gates still apply.
[GitHub transport](../src/loopzero/github.py) owns API access and changed-only
PR writes. [Hosted dispatch](../src/loopzero/hosted.py) and
[eligibility](../src/loopzero/eligibility.py) evaluate trusted review evidence.
In consumer repositories the copyable Actions publisher installs loop-zero at
an immutable upstream revision and reads configuration from the trusted
default-branch checkout. In loop-zero itself both jobs install the package from
that checkout, so publisher fixes take effect when they land.
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
The size budget runs in the local sandbox and in PR, merge-group and main-push CI.
A candidate lineage read that finds zero or several live batch matches is
reread within the current attempt, under a shared deadline. Separately,
candidate attestation restarts as a complete fresh attempt, inside its own
bounded window, on typed races: concurrent source metadata, queue membership,
main-ref drift, or a landed source still in lineage. A closed source counts as
a landing race only when its REST payload confirms the merge; a source-policy
exception counts only when a fresh read confirms a merge at the snapshotted
head. The limits live in [candidate.py](../src/loopzero/candidate.py). Each
attempt resolves identity, lineage and source policy again; no evidence or
approval survives a failed attempt, other failures (including source-head drift)
stop at once, and success requires one complete attestation. Policy evidence is
not atomic across reads. Source PRs skip these attestation retries but keep
their mergeability polling; a source that lands mid-refresh gets its preceding
evaluated verdict back, only while this run's own pending status (by id) is still
newest, outside the failure handler; a late refresh cannot leave it pending or red.
After GitHub acknowledges a pending status, SIGTERM during evaluation triggers a
bounded failure write for that SHA. Once the final write starts, publication
recovery owns its verdict.


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
failures once after five seconds and honor a secondary limit’s Retry-After
within the read budget. An active waiter can resume a fresh poll after a
verified hourly reset within the original deadline. A bounded draft activation
reconciles an ambiguous GitHub reply by reading the exact PR again. An unchanged,
eligible draft gets one backed-off attempt within the same budget. Queue follow
pins the PR target branch along with its source head. Mergify queue reads retry once after two seconds, subject to Retry-After and the
remaining queue wait budget. Calls without a deadline allow less than five
seconds of backoff. See the
[contract's waiter and polling rules](../core/CONTRACT.md#failure).
The optional host [ai-accounts helper](../SETUP.md#several-subscription-accounts-optional)
selects managed credentials and reports usage. It supplies no delivery evidence
and never replays `loopzero` commands.

## Direction and historical evidence

The [September 24 architecture note](design/2026-09-24-delivery-system-architecture.md)
is dated consumer evidence and its accepted principles.

The October 5 direction keeps this guide as the current entry point, preserves
the small delivery toolkit, and makes architecture upkeep part of contribution
and review. Early owning tests, truthful model attribution, independent review
by default with a disclosed same-family fallback, changed-only writes and explicit
ownership remain the design constraints.
The approved execution direction is T3 V2 for coordination, with loop-zero
retaining checks, formal native review under that policy, readiness and verified
merge authority. This boundary is durable; verify runtime compatibility and
deployment status when adopting or updating an external executor.

The [October 5 T3 verification snapshot](design/2026-10-07-deterministic-delivery-design-assumptions.md#t3-verification-snapshot--october-5-2026)
records the runtime evidence behind this boundary: at-least-once notifications,
and watcher limits. A new daemon, scheduler,
reminder issue or metrics ledger would add state without improving the current
authority boundary and is outside this direction.

## Keeping the guide current

Architecture review is event-driven. When changing commands, configuration,
state authority, sandbox/trust boundaries, review/readiness, queue integration,
module ownership or delivery routines, read this guide and the owning sources
before finishing. Edit the guide in the same PR only when a boundary it
describes moved; when `loopzero check` reports a watched change and the guide
still holds, record why in the trailer below. A reviewer checks that claim against the
diff, challenges misplaced ownership and distinguishes shipped behavior from
proposals and dated observations.

The existing repository coordinator or T3 integration maintainer also follows
[T3 upstream review](T3-UPSTREAM.md): event-triggered reading; its
[trigger list](T3-UPSTREAM.md#owner-and-triggers) is the only home for when. This is maintenance triage,
not a new scheduler or permission to adopt upstream policy. Version-specific
comparisons remain dated evidence; the running tools and instructions must be
reconciled with matching source before changing the integration.

Run `uv run --python 3.14 python scripts/check_architecture_docs.py [BASE]`. It compares the base
(default: merge-base of HEAD and `origin/main`) with the working tree, including
new files, and validates local Markdown links and anchors in this guide, the docs
index, README and AGENTS guidance without network access. The repository's
`workflow.toml` runs the default gate (alongside `tach check` against
[tach.toml](../tach.toml) for module dependency boundaries); PR and merge-group
CI enforce it against the event's base SHA using full checkout history.
Push-to-main CI runs `--links-only`, because squash merging
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

## Check stages

Local stages run in Bash POSIX mode with pipefail enabled. A pipeline returns
the status of its rightmost failed member, or zero when all members succeed.
The local shell is part of the receipt and stage reuse fingerprints.

The coordinator resolves stages, aggregates them in order, stops on failure and
verifies that HEAD and the clean/dirty state match their starting values. Only the coordinator
writes the authoritative `checks.json` receipt. Checks lock the task state directory
through execution and receipt publication. A waiter can reuse a newly published
passing receipt when it acquires the lock within 60 seconds and HEAD, settings
and declared inputs match. Private review receipts use atomic replacement
without the check lock. Check receipt writers recheck
HEAD and atomically replace the receipt from a synced sibling temporary file.
Successful keyed stages are checkpointed in the same receipt for reuse.
A successful stage checkpoint preserves a clean pass for the same head,
settings and commands. A failed stage replaces an existing receipt immediately.
Final worktree validation is required before publishing a pass.

Declared `[checks.inputs]` pathspecs opt commands into local passing-result reuse.
Keys bind committed paths/blob IDs, the bytes of the checked-out regular files,
command, installed version and pinned source revision, sandbox configuration and
the resolved base SHA. A stage runs
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
With scratch mounts, a fresh check reruns the prefix through the last stage
without a matching saved result. For same-head resume, checkpoints omit the
reuse key of each stage whose scratch metadata changed or could not be
inspected. Preparations run again on resume. Files they create are already
present when the first check's scratch snapshot is taken, so unchanged stages
remain eligible for reuse. A later stage without an input key or with a lookup
error still forces earlier stages to run. Lookup errors surface in stage order.
Receipts that use saved stage results bind the current head and check settings.

A worker running the entire check is itself the coordinator and writes a
receipt with its own effective settings. Transferred receipts require the same
matching settings fingerprint for local readiness. See
[check receipts with remote execution](../SETUP.md#check-receipts-with-remote-execution).

CI and merge-queue candidates run fresh without a local worktree receipt.
