# Model selection and delegation

Delegate independent, bounded work whose result can be checked. Prefer the
least expensive reliable model; keep small or tightly coupled work inline
when briefing and integration cost more. No delegation quota, line threshold
or exception record is required.

## Coordinator rollover

At a delivery milestone or roughly 150k context tokens (where the host context
hook nudges), launch a successor and post its link. Cap the handoff at 2,000
characters and three numbered first acts: only references needed for the next
action, pending results or questions, binding decision links and the reply
target. Link rules and rulings; omit history and done items. Spend context on
work, not on tool output: first acts read the handoff and only the files it
links, never a chain of older handoffs; bound every read (T3 thread reads at
most 2 items of 600 characters, helper and shell output at most 2,000
characters, no full activity, roster or schedule listings); repeat a call that
echoes large state, such as schedule updates or capability listings, only when
that state changed. The launch prompt names only the role, handoff path and
reply target. Parked work and its dispositions live in one durable table (the
owning tracking issue or one file) that every handoff links instead of
re-listing. Transfer recurring tasks and pending wakeups and reconcile live
children with the successor before stopping; a launch notice alone transfers
nothing. Every retiring owner or helper starts heavy or long-running jobs
as a transient user service, e.g. `systemd-run --user --unit=NAME
--property=RuntimeMaxSec=LIMIT --collect bash -c 'CMD >LOG 2>&1; echo $? >LOG.exit'`,
and names the unit, log and exit file in the handoff (unit gone without an exit
file means killed). `setsid` or `nohup` jobs die at settle or retirement;
never retire with an attached live job, because retirement ends its process
tree. Changed decisions name the instruction superseded. After transfer,
forward late results once with their original task reference, make no new
decisions and retire so sends or schedules
cannot revive the session. After delivery, roll over before monitoring or
unrelated follow-ups; do it yourself. A bare "continue" means continue there.
Workers report on the owning PR or issue, directly or through the parent;
coordinators reconstruct delivery from live remote state.

**Provider-portable handoff.** If an owner's provider is exhausted or unavailable,
use a fresh successor on another provider or model only within an explicit
standing route; preserve commissioned choices and runtime permissions. Otherwise
report the unavailable route and wait for owner direction. Confirm the old
writer and its tool tree have stopped or are fenced before the successor acts.
Keep the handoff current after consequential decisions, within the caps above;
recovery cannot depend on a failed session's final summary. Link owning issue/PR,
evidence, authority and holds; worktree/branch/head/dirty state; checks and
interrupted tool outcomes; live child/task IDs, schedules and wakeups. Include
the next authorized action. Git/GitHub and the handoff own state, not provider
memory or resume tokens.
The successor first reconciles live remote state and uncertain external effects;
a lost tool response is not proof the write failed. Adopt partial work without
resetting or cleaning away the diff, and never replay whole commands blindly.
Revalidate holds and recipients, then confirm child and wakeup ownership transfer
before retiring the predecessor. Start with read-only coordination; if the new
family writes, retain every writer's truthful attribution and recheck independent
review eligibility. A provider change grants no new authority or review exception.

## Team roles

A long-running team gives each role one current owner. Owners read state from
the owning issue and the live decision record, not from earlier sessions.
Resolve every recipient when sending: the coordinator from the active delivery
schedule, a campaign owner from its tracking issue, the decisions owner from
its newest live generation. Never use a remembered ID; escalate ambiguity once.
For important T3 messages to an owner, verify receipt with `t3_thread_read`;
a successful send alone does not prove delivery.
A schedule firing or a relayed approval grants no new authority.

- **Coordinator:** owns one repository's delivery, main health and shared
  delivery infrastructure; drains ready backlog toward the current goal, routes
  incidents and dispatches workers and reviews. May delegate routine dispatch
  and integration to named executors; it keeps main health, merge freezes and
  holds, shared delivery infrastructure and production observation. Reports to
  the decisions owner. Its reconstruction from live remote state includes open
  PRs with no push, comment or review for 6 h or more whose owner session has
  ended: it adopts each or names a next-action owner.
- **Decisions owner:** the only role the human owner talks to. Resolves owner
  decisions within delegated authority, records each with its source where it
  binds, and routes asks to the current responsible owner. Settles related cases
  in one decision and ends each ruling with the next executable action and what
  needs no further consult.
- **Campaign owner:** delivers one commissioned campaign while its tracking
  issue is open, and ends when it closes. Self-dispatches within approved scope
  and allocated host capacity without per-launch coordinator permission. Reports
  on the tracking issue; messages the coordinator only for blockers.
- **Worker:** one bounded delivery under a brief (see
  [Briefs and ownership](#briefs-and-ownership)); returns acceptance evidence
  or one named blocker. An unattended worker never uses an ask-the-user or
  request-input tool: send one message with any blocker or question to its
  parent (or the owner named in the brief), then end the turn. A child that
  asks directly is treated as blocked.
- **QA:** a round files reproducible defects in commissioned states, with every
  occurrence. A retest checks every recorded occurrence at the exact merged
  revision; the issue closes only on PASS, so repairs use `Refs`. A missing
  fixture or credential is a blocker, never a PASS.
- **Monitor and watchdog:** detect regressions and idle work; send one packet
  or nudge per new finding, and never decide for an owner. A periodic review
  first asks why an existing mechanism failed and repairs it before adding one.

Each acceptance slice has one dispatch authority: the campaign owner within its
commission or, for the remainder, the coordinator or the dispatcher it names.
Record that partition on the existing owning issue or PR. Before commissioning
pickup, that authority records the worker, branch/worktree or task reference,
and next action there. Reconcile overlaps before launch; no competing owner
launches. A claim comment is evidence, not an atomic lock: one authority per
slice prevents races.
Do not add a claim ledger, service or label system.

Fresh bounded runs send nothing when evidence is unchanged. Workers report deltas
on the owning PR or issue, directly or through the parent, and message the parent
mid-task only for a blocker. Combine notices to one recipient into one packet;
request an acknowledgement only when receipt changes the next action. Blockers
and changed holds still go at once. Monitors and watchdogs keep sending one
packet or nudge per new finding. Count productive work by verified activity (completed tool
calls or output on the deliverable), including nested reviewers; report test-slot,
review, queue and hold waiters separately, excluding them from productive counts.
Children waiting on a question or approval are waiters, not productive work.

**Owners decide without the decisions owner.** Within existing commissions and
grants, owners act, record the decision on the issue/PR or their own log, and
tell the decisions owner in their next routine update:

- Re-own a stalled child under its existing brief and grant.
- Rebase onto current main and retest within existing proof and review budgets;
  product conflicts or new migrations go to the decisions owner.
- Close non-QA work whose full acceptance is proved; preserve separate QA,
  umbrella and hold conditions.
- Revert their own failed trial to its previously authorized setting under its
  recorded rollback terms.

Reserved for the decisions owner (or the human owner): merge/ready/queue GO for
each PR@head; spending beyond current accounts; auth; public API; destructive
migrations; CI/required checks; production lease; and anything on the standing
never list. Ask for merge GOs once per head, only after its check receipts
exist, batching every head that is ready into one request. Routine decisions
grant no exception to these reservations or existing holds.

**Holds and freezes.** The live decision record is the only authority: one line
per merge freeze, production window or hold ("Merge freeze: …") naming the
hazard, its owner, the end condition and when it is next re-decided. A clock
time may trigger that re-decision; without a named hazard or owner preference
it is never an end condition or a start floor. Changing the line is the
decision. Broadcast each change to every active owner, resolved when sending,
only so they reread it; a missing message never extends a hold. Every owner
reads the line before holding a merge or release; an unreadable or ambiguous
line blocks, and expiry or silence grants nothing. Renewing a hold names the
hazard still present and why waiting is safer than the available route;
restating the original reason is not enough. A merge freeze pauses merging
only, never building, checks or review.

**Release plan review.** Before owner approval for a production window, verify
every named backup/database provider, recovery point, CLI and identity against
live infrastructure at review time; defer no check to the window. Prefer
agent-readable evidence; avoid short-deadline human receipts; name any
human-only receipt in advance with that person's confirmed availability. Before
setting the production fence, require the full product/browser suites to
actually run green (executed, not skipped) on release head combined with current
main; never first-run in-window. Reconcile the newest scheduled main run of
those suites; explain or fix red first; refresh if inputs change. Keep the
production fence in place before landing; never merge early solely to obtain
that suite run. Set per-step caps from measured queue, deploy and abort
durations. Waiter expiry is not dequeue: verify queue state. Keep the approved
plan's abort order unchanged. Compare candidate and control on main by exact
signature and mechanism, not aggregate red counts; record immutable heads, workflow
version, run/job/test, assertion, setup and environment. A shared failure on
comparable main is `KNOWN-RED-ON-MAIN`: record its issue, repair owner and expiry
in the release packet. That attribution alone does not veto release; existing
release gates still apply. Candidate-only, new or different signatures and
missing, cancelled or skipped coverage remain unresolved, never green. Green main
does not prove a candidate regression. Required checks stay strict: attribution
never overrides `loopzero ready`/`merge` or required CI. Any narrow exclusion to
make a required check nonblocking must be changed and reviewed in its owning
consumer. Quarantine only after diagnosis; name the exact test/signature, cause
evidence, issue, owner, fixed calendar expiry and exit criterion. Keep it running,
nonblocking and reported. Expiry returns it to blocking unless renewed with
evidence. Prefer fixing; no suite-wide skip, retry-until-green or hidden xfail.

**Liveness.** Owners route and workers build: an owner commissions build work to
a worker and never builds in a turn that answers a message. Measure progress by
work output on the commissioned deliverable (a commit, push, PR or checks
started on a new head), never by turns. Acknowledgements, unrelated commits,
[unnecessary gate reruns](../core/CONTRACT.md#blockers) and uncommitted changes older than 60 minutes are
not output; workers commit at every green step and push at least hourly. A
stream holding addressable work is stalled after 60 minutes without output or
after three acknowledgement-only or empty turns in a row: push it once with an
instruction to act in that turn, naming the next command. A campaign owner
holding addressable build work never ends a turn with only an acknowledgement.
At every wake and handover, parents check live children for waiting questions or
approvals by reading their thread state, including the pending-request count and
pending-request list. Answer within authority or route the request to the owner.

**Event-triggered review.** Review progress when evidence shows something is
off, not on a clock. Host policy decides which signals wake an owner (for
example eligible work waiting while slots are free, a Liveness stall, a
merge-authorised green PR left unqueued, or a hold past its re-decide time) and
wakes only the owner concerned, with that one finding; an unchanged finding
does not wake again, and an unreadable source is unknown. On a wake, read every
claim the decision rests on from the remote: reported work shortages, "in the
queue", the change class from the PR file list. A stale draft is not
ownership. Record one entry: the outcome and its current condition, the
obstacle, the next step with its predicted effect and check time, and at that
check the actual result. Judge reviews by changed next actions and shortened
blockages; a daily check keeps or reverts each adjustment against its numbers.

**Retrospectives.** The outcome owner records a retro, in loop-zero and in
consumers, when any of these occurs:

1. A production or delivery outage (one that stops reviews, the queue or
   deploys for everyone), a failed or unintended deployment, or an executed
   breach of an explicit authority or protected-checkout rule. A demonstrated
   main defect counts at once; otherwise a required or correctness-nightly
   check still failing on main 60 minutes after its first failure.
   Administrative monitors and review-eligibility jobs do not count.
2. An executed corrective rollback of code, a deployment or configuration.
   Unused contingencies and planned switch-back tests do not count.
3. A hold that misses its re-decide time or restricts a commissioned outcome
   for 24 hours; renewals do not reset its age, and a retro never lifts it.
4. The human owner, or the decisions owner for them, corrects an evidenced
   wrong claim, unauthorised restriction, lost ownership or failed
   continuation. Approvals, priorities and plain questions do not count.
5. Addressable work still stalled 60 minutes after its Liveness push;
   legitimate dependency, queue and hold waits do not count.
6. A second independent occurrence of the same workflow failure (a stop,
   wait, corrective intervention or quota hit), linked to the first.

Routine releases need none. Count independent causes once, never retries or
collateral. Write one `Retro:` comment per causal episode on its owning issue
or PR, after recovery, and update it with new evidence: what happened and for
how long; expected versus actual; the existing mechanism that should have
caught it, or "none: capacity or uncertainty"; and up to two actions (a repair,
a deletion or a check inside an existing mechanism) with owner, observable
result and check date, or why none remains. Workflow health still governs
any change.

Close the loop on a schedule that host policy sets. A daily check rules on
every action whose check date has arrived: keep, revert or extend once with a
reason. It also pushes the owner of any trigger event still missing its retro.
An action more than a day overdue counts as an occurrence under trigger 6. A
weekly audit covers every dimension of the fleet against its sources, rechecks
whether earlier actions were shipped, used and effective, and names at least one
mechanism to delete, or why none qualifies.

**Goal first.** Each owner holds its commissioned outcome (for example
"ordinary changes reach users") and treats its plan as replaceable; finishing
a step, package or subtask never redefines success. Within the human owner's
explicit priorities, recorded by the decisions owner in the live decision
record, work on whatever most limits that outcome now: implementation,
readiness, CI, review recovery or deployment. Separate constraints (owner
authority, a demonstrated hazard) from assumptions (an inherited sequence, a
preferred tool, an estimated start time); repetition does not make an
assumption binding. Two repair rounds on one mechanism or defect class, or two
stops caused by tooling rather than the product, mean reassessing the route:
prefer an existing simpler route to another isolated fix or check. Measure
progress by what became usable, unblocked or safer. When a subtask closes, its
owner keeps the remaining outcome or hands it over on the owning issue.

**Workflow health.** Keeping delivery lean is a standing duty, not a tier. A
broken workflow that blocks delivery is the bottleneck: repair it at once.
Otherwise improve it in the background, never on the path of a release or
recovery, and only for a problem that occurred at least twice: a stop, a wait,
an owner intervention or a quota hit, each linked to its record. Each change
names those occurrences, prefers repairing or removing to adding, and is
reverted if the problem recurs or the change causes new stops. During an
incident, change the workflow only to remove or bypass a step.

**Done and unknown.** Report merged, deployed, visible to users and still
restricted as separate claims; an outcome is done when its user-facing check
passes and every remaining restriction has an owner. Acceptance for delivery
infrastructure exercises its real consumer, including switching to the change
and back. A failed observation (empty logs, an unreachable check, a broken
evidence file) is unknown: never a PASS, and not by itself proof that the
product failed. The owner decides whether that unknown blocks the next
operation or another observation suffices.

Bound dispatch by shared resource slots and host guards, never by agent count.
Implementation work does not require or reserve a test slot; a heavy command
(more than one core for more than a minute) acquires one only while it runs.
Host slot counts, load thresholds and quota guards belong in host policy.
Tune dispatch from useful output, eligible work supply, review/test wait, rework
and merge service; reduce new commissions when downstream work piles up.
Give every addressable issue a next-action owner or a named dependency. Work on
the current bottleneck gets dispatch and shared test slots first; other running
work finishes or checkpoints its current heavy command and releases its slot.

**Rollover.** [Coordinator rollover](#coordinator-rollover) applies to every
owner role. Before acting, the successor revalidates recipients, schedules and
carried decisions against live sources and reports what it cannot confirm as a
blocker. Peers resolve the current owner when sending, so a rollover sends no
routing notices; state broadcasts still reach every owner. The successor retires
the predecessor in the turn it acknowledges or, while the predecessor still
parents live delegates, when the last one finishes.
A recurring schedule belongs to its role owner (the coordinator for delivery),
never to a worker or one-day owner; workers get one-shot wakeups only. The
successor rebinds role schedules by ID. Each run of a recurring task first
checks that its previous run's PR merged and adopts that PR if not.

## Resolve the model before delegation

Read `models.toml` at the **task repository's worktree root**, never another
project's defaults. Report missing, malformed, unknown-version or ambiguous
configuration rather than inventing defaults; preserve explicit user choices.

1. Resolve category → profile → provider, model and effort. Categories are
   starting points; choose another profile if none fits or the task warrants it.
   Settled bounded implementation starts at `bounded_implementation`.
2. Use the default or explicitly select a configured alternative allowed by
   provider preferences. Alternatives are choices, not automatic retries.
   Distinguish models when several share a provider.
3. Check actual runtime support and pass **both model and effort explicitly**.
   Providers are not executable names; aliases resolve in the runtime.
   Use a self-contained brief when model selection requires fresh context.
4. Report unavailable routes or rejected efforts. Explicitly choose and announce
   a suitable configured substitute only within authorized choices. Preserve an
   explicitly requested model; ask for direction if no authorized route remains.
   Runtime permissions still apply.

**Provider preferences in `models.toml` limit every choice.** When they leave
no available route (for example every audit route is OpenAI and OpenAI is at its
usage limit), report it and ask the owner whether to wait or explicitly approve
a substitute; availability of another provider is not permission. Checking
audit findings with the other provider before acting is a separate step.

## Profiles are starting points

Profile names describe task shape, not measured cost or speed. Preserve owner
assignments; reassess with evidence and owner direction. Weigh uncertainty,
consequences and verification quality.

Reassess when scope grows or results are unreliable. Fix environment failures
before escalating models; escalate substantive difficulty directly to a suitable
profile. Avoid blind retries or repeating successful work for reassurance.

## Author lineage and independent review

The contract's [review budget](../core/CONTRACT.md#review-budget) owns
authorship and reviewer selection. Keep one writing family by default; the other
can advise read-only. Before committing, record every actual writer in truthful
`Co-Authored-By` trailers. The parent committing a delegate's patch does not
replace that author, and applying a supplied patch verbatim is not writing:
credit the patch's writer, not the applier.

Without the contract's
[owner-approved exception](../core/CONTRACT.md#owner-approved-contributor-review-exception),
retain all writers' attribution through amendments and squashes. If mixed
authorship leaves no independent configured reviewer, review fails closed:
wait for the owner. Do not re-author work or drop trailers to regain eligibility.
Read the exception before mixing writing families. Formal delivery review is
`loopzero review`.

## Briefs and ownership

Give each writing delegate its own `loopzero start` worktree outside any
protected production checkout and a self-contained brief:

The first shell command enters that task tree; repeat `cd <own-task-tree> &&`
at the start of every shell command, including reads and background launches.
Set the launcher's working directory too when supported; never rely on the
inherited project root.

```text
cd <own-task-tree>
Objective: <one bounded outcome>
Context: <source text, decisions, files, and requester's wording/link authorizing the finish point; sandboxed delegates usually lack network/gh>
Allowed paths: <owned scope>
Acceptance evidence: <observable proof for each criterion>
Model/effort: <resolved from the task repository's models.toml>
Effort policy: Children inherit this policy; return to the parent before raising effort or launching a stronger model.
Launch policy: Children inherit this rule: never approval-required; use auto only for read-only children and say "read-only, do not edit" in the brief; a child that runs commands or edits inherits the parent's mode, because auto can stall on approval review. After launch, verify pendingRequestCount 0 and a completed tool call.
Waiting: One blocking wait: Codex exec/write_stdin yield >=30 s (>=120 s for builds, tests, CI, review, ready or merge); shell `until <done>; do sleep 30; done`. Claude: background notification or t3_background_wait. Read the completed result once; never use t3_thread_read/task_status for liveness.
Output: Cap at the producer: REST `gh api --jq` selected fields, shell `| head -c 2000`; t3_thread_read limit <=2, maxCharsPerItem <=600.
Shell commands: Keep cleanup/removal and commit operations separate and simple; do not hide commands in scripts to evade approval review.
Verification: <focused owning tests and coverage; available proof methods and their task-scoped authorization within runtime permissions>
Integration executor: <named executor for returned heads and one owner of any readiness/merge waiter; parent retains acceptance responsibility>
Parent verification: <broad suites and loopzero check; delegates do not repeat these or start reviewers>
Follow-ups: <ordered list of 1–2 already commissioned, bounded items, or none; assign only to delegates that can both write Git and post to GitHub themselves>
Return: <authorized finish: evidence, readiness, or merge; deployment needs its own commission>. Continue until that boundary or a named blocker needs the parent; then follow only the preassigned list, otherwise return. Delegates that cannot do both: write Git and post to GitHub themselves (including sandboxed delegates, delegates without GitHub access, or delegates unable to write the shared Git directory) get no preassigned follow-ups and return after one item via their final Report. A progress summary is not completion.
Report: Final message to the parent, which posts it on the owning PR or issue (post directly only when the delegate has GitHub access): changed files, evidence, check results, deviations, open questions and next action. Message the parent mid-task only for a blocker.
```

Example using those fields, with an illustrative requester instruction:

```text
Context: Requester said “prepare the fix for review; leave it unmerged” (<request link>).
Verification: Focused regression and browser observation on the local fixture, within existing task permissions; report unavailable proof.
Integration executor: Parent integrates and owns the sole loopzero ready --wait; child starts no waiter.
Return: Tested changes and evidence to parent; parent finishes through readiness, leaving merge to the requester.
```

The source request supplies authority; neither this example nor a finish label
grants it. An unattended child never uses the ask-the-user or request-input
tool: send one message with a blocker or question to the parent or current
decisions owner, then end the turn. The owner uses the available harness question
tool with the PR/task link, exact decision and recommended option. If unavailable
or unanswered, preserve authorized work and report the blocker and next owner;
silence is not approval. T3 watches are wakeups, not additional readiness waiters;
the watch owner unwatches before handing control to a person.

Before each preassigned follow-up, re-read live issue/PR state and dispatch
ownership, record the finished item's current head and evidence directly on its
owning PR or issue, then start a fresh task worktree.
Do not self-claim arbitrary global backlog. On exhaustion or a blocker, return
next-action evidence to the dispatch authority through the existing completion
mechanism and owning PR or issue.

### Launching a background CLI delegate

Use the resolved model and effort, an explicit sandbox (`workspace-write` for
edits, `read-only` for investigation), `--cd`, closed stdin and a log.
Read the brief through an **absolute path**: relative reads around background
`cd` can produce an empty prompt.

```sh
codex exec --model <resolved-model> -c model_reasoning_effort=<resolved-effort> \
  --sandbox workspace-write --cd <worktree> \
  --output-last-message <final-message-file> "$(cat /absolute/path/to/brief)" \
  </dev/null ><log-file> 2>&1 &
echo $! ><pid-file>
```

Record `$!`; use `kill <pid>` to cancel, never `pgrep -f` or `pkill -f`:
patterns also match the waiting shell and can wait forever. Block once with
`while kill -0 "$(cat <pid-file>)" 2>/dev/null; do sleep 30; done`, using the
brief's runtime-specific wait above; then read `<final-message-file>` once.
Never use `t3_thread_read` or `task_status` for liveness.

App-orchestrated delegates (for example T3 `delegate_task`) launched in an
approval-requiring mode stall unattended at their first tool call, while task
status still reads running. For an unattended read-only consult, choose the
narrowest mode within your own authority that runs reads without prompts (T3:
`runtimeMode: auto`) and state "read-only, do not edit" in the brief. That is
an instruction boundary, not an OS sandbox; when writes must be impossible,
use a CLI delegate with `--sandbox read-only`.

Launch independent tasks in small concurrent batches within the host's guards
(host policy). Retain each child task ID and verify every child's first completed
tool call and zero pending approvals; verify the batch concurrently, without
waiting for one child to finish its task before launching the next. An unverified
child is not proven productive capacity.

Running or working status does not prove progress. Before ending the turn
after a launch, read the delegate's activity: a completed tool call and no
pending approval (T3 `t3_thread_read`: `pendingRequestCount` 0). Answer an
approval only within your authority; otherwise cancel, relaunch correctly or
report the block. When a verified child stops producing output, re-read its
pending approvals (T3 `pendingRequestCount`) and terminal task state, including
provider/capacity failures. Recover or report the blocker in that turn using the
same authorized routes; a launch-time check is insufficient. Inspect what ran
before resuming interrupted work. Since T3 0.1.6 a later successful newest run
releases held automation messages; held human messages and runs after an
explicit stop stay held (`queueHeld=true`). Only then save each held message's
full text, cancel it with `t3_queue cancel`, resend the still-needed messages as
one fresh message and verify receipt. For a terminal delegated task, launch
a new task with the prior brief and result rather than queueing another message
to it. Since 0.1.7 PR watches survive restarts and are rebuilt from persisted
links; re-watch only a watch that is missing.
Before yielding on any delegate or long command, arrange a
wakeup whose timeout covers the awaited event: a completion notice plus a
fallback timer, or a bounded blocking wait. Rely on T3 delegate completion
delivery; when this turn needs a thread's result, call `t3_thread_wait`
(`untilDelegatedWorkSettled:true` when nested work matters), follow its `next`
and read the final result once. A finite retry uses a one-time schedule
(`schedule_task` with `schedule:{type:once,runAt:<ISO time>}`), not an interval.
Before arming or rearming a wait, reconcile the awaited task's existing terminal
result and current remote state; after a superseding message or restart, check
again before concluding "still waiting".

Split oversized scope in the plan rather than imposing brief line limits. Resume
unfinished delegate sessions instead of restarting; reassess repeated repairs as
in [Goal first](#team-roles). A repair brief lists every unresolved review
finding and deterministic test failure on the failed head; an unrelated repair
does not fix one.

Consult and artifact reviews outside a PR use the same budget: one full review, then at
most one delta per artifact. Reviewers classify each finding as blocking (it can cause an
unsafe or unintended effect, a missed stop or a false admission) or follow-up. A blocking
finding states the concrete failure scenario, its consequence and the smallest correction;
a patch is optional. Without a failure scenario it is a follow-up. The owner checks its
premise against the actual system (independent reviewers can share a wrong premise); the
implementer integrates the correction and proves the behavior, without reopening review.
Follow-ups wait.
Builders change only what the brief names and keep regenerated evidence inside its
declared allowlist; a scope-only or metadata finding never earns another round.

Read-only work may share stable source. Sequence overlapping writers or
integrate deliberately; never run them in one checkout. Do not pre-clear file
overlap by message: rebase and the queue surface conflicts and the later lander
rebases; ask once, on the PR, only when both change the same function. Respect
concurrency limits and avoid competing suites whose temporary-directory cleanup
can interfere. Any command using more than one core for more than a minute runs
under the repository's shared resource/test slots, including builds, type checks,
formatters and parallel tests. When the host is saturated, shed out-of-slot CPU
commands before shedding agents; preserve their work. Grant only a needed cache
directory (`--add-dir ~/.cache/uv`);
shared `/tmp` caches can exhaust host inodes. If a linked worktree's shared Git
directory is outside the writable sandbox, leave commits to the parent. Every
dependency names its producer and the producer's next action. A consumer opens
its PR against the producer branch once its checks pass, is reviewed there and
is retargeted when the producer merges. Retarget stacked PRs before deleting
their base.

For a stack, agree the configuration with the coordinator: copy the trusted
base's complete `workflow.toml` to `.loopzero/stack-workflow.toml` and change
only `[repo].base` to the producer branch (for example `base = "lz/producer"`).
Keep every check, required CI name, publisher and reviewer setting intact;
fetch the producer branch into `origin/<producer>` first. From the consumer's
own task tree, use the same file throughout:

```sh
loopzero --config .loopzero/stack-workflow.toml check
loopzero --config .loopzero/stack-workflow.toml pr
loopzero --config .loopzero/stack-workflow.toml review
```

`--config` uses explicitly supplied settings instead of trusted-base pinning;
the coordinator must verify the copy and its one-field difference. Do not
retarget to main just to launch review. After the producer lands, retarget the
consumer to main and return to the canonical configuration. If integration or
rebase changes the head, rerun checks and review under the existing lineage
budget before readiness and merge; an earlier approval is not evidence for the
new head.

Nested background results may reach the top-level session instead of their
owner: run nested reviewers in the foreground or forward results promptly.

The parent owns integration and acceptance, and may name an integration executor
in the brief: inspect returned diffs and evidence; worker success claims are not
proof. Route returned heads to that executor. Run decisive tests unavailable to
the delegate before review, and required broad verification and `loopzero check`
under the [receipt reuse rule](../core/CONTRACT.md#blockers). If declared checks do not isolate host tools, run a
focused restricted-PATH probe and report the limitation. Only sandboxed
`loopzero check` counts as delivery proof; continue under the
[contract](../core/CONTRACT.md).

## Configuration and upgrades

Follow [T3 upstream review](T3-UPSTREAM.md) weekly during active use and before
relevant upgrades or compatibility repairs. Compare the running environment's
tools and instructions with version-matched source before adopting upstream
developments; discovery does not authorize an upgrade or change delivery policy.

[models.toml](../models.toml) owns selections; skills link here. Consumers
maintain their own copy. Version 1 has profiles with `provider`, `model`, `effort`,
optional same-field alternatives, and category-to-profile mappings. Verify
selected entries are complete and referenced profiles exist.

Agents read it; the CLI does not parse it, schedule work, change the parent model
or configure `loopzero review`. New profiles, models and efforts need no
routing-code changes or separate catalog validator.

Upgrade using existing PR evidence: first-pass acceptance, rework, elapsed time
and observed usage. Reassess serious failures immediately; prefer repeated
evidence to isolated costs. Add no ledger, spending quota or automatic
benchmark gate.
