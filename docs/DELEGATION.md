# Model selection and delegation

Delegate independent, bounded work whose result can be checked. Prefer the
least expensive reliable model; a worker keeps small or tightly coupled work
inline when briefing and integration cost more (owners commission build work,
see [Liveness](#team-practices)). No delegation quota, line threshold
or exception record is required.

## Coordinator rollover

This section applies to every owner role. Each role runs in one permanent T3
thread that resets its session in place. At a delivery milestone or the host
context hook's nudge, overwrite the role's one fixed handoff file (never a new
file per reset), then send the same thread
`freshContext: true` with only "Read <handoff path>". Another owner can send
the reset if that thread cannot act. Keep the handoff current after consequential
decisions so recovery can use it if the session ends unexpectedly. The thread keeps its
schedules, PR watches and delegate completions, so a reset rebinds, retargets
and settles nothing; it changes no address, so it sends no routing notices,
though state broadcasts still reach every owner. Before resetting, start heavy or long-running jobs as
described in [Long-running jobs](#long-running-jobs) and name the unit, log and
exit file in the handoff.

### Handoff and output caps

Cap the whole handoff at 2,000 characters, holding active links and up to
three numbered first acts: only references needed for the next action, pending results or questions,
binding decision links and the reply target. Link rules and rulings; omit
history and done items. Parked work and its dispositions live in one durable
table (the owning tracking issue or one file) that the handoff links instead
of re-listing. Handoffs do not carry owner orders: after a reset the owner reads
open `p0` issues. Long schedule bodies live in files their prompts point to.
Changed decisions name the instruction superseded.

Spend context on work, not on tool output: first acts read the handoff and only
the files it links; bound every read within host limits, with no full
activity, roster or schedule listings; repeat a call that echoes large state,
such as schedule updates or capability listings, only when that state changed.
A delegate notice that only reports work still running needs no action: end that
turn without calls. T3 watches are wakeups, not readiness waiters: do not
watch a PR whose readiness or merge a delegate or your own `--wait` already
reports, and unwatch before handing control to a person. After delivery, reset
before monitoring or unrelated follow-ups. A bare "continue" means continue
there. Owners reconstruct delivery from live remote
state, revalidate recipients and carried decisions, and report what they cannot
confirm as a blocker.

### Provider-portable handoff

If an owner's provider is exhausted or unavailable,
move the role to a new thread on another provider or model only within an
explicit standing route; preserve commissioned choices and runtime permissions.
Otherwise report the unavailable route and wait for owner direction. Confirm the
old writer and its tool tree have stopped or are fenced before the new thread
acts. Link owning
issue/PR, evidence, authority and holds; worktree/branch/head/dirty state;
checks and interrupted tool outcomes; live delegate/task IDs, schedules and
wakeups. Include the next authorized action. Git/GitHub and the handoff own
state, not provider memory or resume tokens.
The new thread first reconciles live remote state and uncertain external
effects; a lost tool response is not proof the write failed. Adopt partial work
without resetting or cleaning away the diff, and never replay whole commands
blindly. Revalidate holds and recipients, rebind the old thread's schedules and
wakeups, and wait for each of its running delegates to finish (their
completions still go to it: `t3_thread_wait` or a one-shot wakeup), reading each
result once; then archive the old thread. Whenever a send, schedule or delegate
completion revives it, it ends that turn without acting or replying;
never send through it.
Start with read-only coordination. A provider change grants no new authority
or review exception.

### Long-running jobs

Start a transient user service. Use a unique unit name and fresh absolute paths
for `LOG` and `LOG.exit` each run. Jobs detached with `setsid` or `nohup` die at
settle or retirement.

```sh
systemd-run --user --unit=NAME --working-directory="$PWD" \
  --property=RuntimeMaxSec=LIMIT --collect bash -c 'CMD >LOG 2>&1; echo $? >LOG.exit'
```

The unit starts with the user manager's environment. Pass needed variables with
`--setenv=NAME=VALUE` or use absolute tool paths.

Wait for the exit file or for the unit to stop:

```sh
until test -f LOG.exit || ! systemctl --user is-active --quiet NAME; do sleep 30; done
```

Report completion only once `LOG.exit` exists. After the wait, a missing file
means the job failed (killed, timed out or cancelled). To cancel, run
`systemctl --user stop NAME` only if the exit file is absent.

### Leftover cleanup

The named retention job must confirm a pushed branch, merged or closed PR,
retention of any evidence required by the delivery contract, clean tracked/untracked
Git state, an empty shared stash, inactivity, no unarchived T3 thread binding
where T3 is used, and its host age/size policy with a 24-hour mtime/ctime idle
floor. Honor host execution approval rules; use dry run until execution is
authorized.

## Team roles

A long-running team gives each role one current owner. Owners read state from
the owning issue and open `p0` issues, not from earlier sessions.
Address each role at its permanent thread; an in-place reset keeps that
address, and a campaign owner is found from its tracking issue. Only a
[provider move](#provider-portable-handoff) changes an address: after one,
resolve the recipient again instead of using a remembered ID, and escalate
ambiguity once.
For agent-to-agent sends to a running Claude thread, use queue mode unless
urgent; steer can cancel the receiver's in-flight tool call.
For important T3 messages to an owner, confirm arrival with `t3_thread_read`;
a successful send alone does not prove delivery.
A schedule firing or a relayed go-ahead grants no new authority.

- **Coordinator:** owns one repository's delivery, main health and shared
  delivery infrastructure; drains startable backlog toward the current goal, routes
  incidents and dispatches workers and reviews. May delegate routine dispatch
  and integration to named executors. It tracks required CI on the exact main
  head, keeps merge-authority holds and owns shared delivery infrastructure
  and production observation. Reports to
  the decisions owner. Its reconstruction from live remote state includes open
  PRs whose [PR owner](../core/CONTRACT.md#the-six-steps) session has ended: at each dispatch pass it adopts each or names a
  next-action owner.
- **Decisions owner:** the only role that takes the human owner's orders and
  asks the owner decisions; the status thread only reports to the owner.
  Resolves owner decisions within delegated authority, records each with its
  source where it binds, and routes asks to the current responsible owner.
  Settles related cases in one decision and ends each ruling with the next
  executable action and what needs no further consult. Its whole job: take
  orders, ask real decisions, and rule on escalations. When the owner is away
  and the answer is easy to predict, it decides on the owner's behalf. Format
  owner-facing asks and rulings under [Reporting](../core/CONTRACT.md#reporting).
  It keeps no watch and sends no status.
- **Status thread:** a schedule that host policy sets runs it as a fresh
  session. Each run
  rebuilds from GitHub and T3 records, tells the human owner what is done, not
  done and wrong under [Reporting](../core/CONTRACT.md#reporting), and sends the
  decisions owner any `p0` with no commit, PR update or result within the host
  policy's limit to re-home. The decisions owner owns the schedule
  but reads no runs.
- **Campaign owner:** delivers one commissioned campaign while its tracking
  issue is open, and ends when it closes. Self-dispatches within approved scope
  and allocated host capacity without per-launch coordinator permission. Reports
  on the tracking issue; messages the coordinator only for blockers.
- **Worker:** see [Worker](#worker).
- **Lean warden:** keeps the delivery system lean: loop-zero improvement,
  host delivery tooling (test slots, remote hosts, CLI upgrades) and workflow
  health. Reports to the decisions owner.
- **Monitor and watchdog:** detect regressions and idle work; send one message
  or nudge per new finding, and never decide for an owner. A periodic review
  first asks why an existing mechanism failed and repairs it before adding one.

### Issue closure owners

- **Owner orders:** label or open a `p0` issue for the human owner's order,
  name its owner in a comment and reply with its link in the same turn.
  PRs use `Refs`; close only with a comment holding the result or blocker.
- **QA:** file reproducible defects in commissioned states with every occurrence.
  Retest every occurrence at the exact merged revision. Repairs use `Refs`;
  close only on PASS. Missing fixtures or credentials block PASS.

### Worker

One bounded delivery under a brief (see
[Briefs and ownership](#briefs-and-ownership)); returns acceptance evidence
or one named blocker. An unattended worker never uses an ask-the-user or
request-input tool. Workers send no mid-task messages to their parent. If a
blocker or question needs the parent's input, the worker returns it at once in
its final message. A delegate that asks directly is treated as blocked.

### Dispatch authority

Each task has one dispatch authority: the campaign owner within its
commission or, for the remainder, the coordinator or the dispatcher it names.
Record that partition on the existing owning issue or PR. Before commissioning
pickup, that authority records the worker, branch/worktree or task reference,
and next action there. Reconcile overlaps before launch; no competing owner
launches. A claim comment is evidence, not an atomic lock: one authority per
task prevents races.
Do not add a claim ledger, service or label system.

### Team practices

Fresh bounded runs send nothing when evidence is unchanged. Workers report deltas
on the owning PR or issue, directly or through the parent, and report a blocker
as in [Worker](#worker). Combine notices to one recipient into one message;
request an acknowledgement only when confirming arrival changes the next action. Blockers
and changed holds still go at once. Count productive work by verified activity (completed tool
calls or output on the deliverable), including nested reviewers; report test-slot,
review, queue and hold waiters separately, excluding them from productive counts.
Delegates waiting on a question or permission prompt are waiters, not productive work.

**Owners decide without the decisions owner.** Within existing commissions and
grants, owners act and record the decision on the issue/PR or their own log:

- Re-own a stalled delegate under its existing brief and grant.
- Integrate base changes under the [review budget](../core/CONTRACT.md#review-budget)
  and [receipt reuse rules](../core/CONTRACT.md#check-reuse-and-delivery-priority).
  Product conflicts or new migrations go to the decisions owner.
- Close non-QA work whose full acceptance is proved; preserve separate QA,
  umbrella and hold conditions.
- Revert their own failed trial to its previously authorized setting under its
  recorded rollback terms.

**Merge authority.** See the contract's [merge authority](../core/CONTRACT.md#merge-authority).

Exclude a known non-critical shared check failure the change did not cause,
after checking no protected check (one the consumer designates as protected,
such as a release or deployment check) is involved, on its second strike in 7 days
or when it also fails on main, then diagnose; name the exact test/signature,
evidence, issue, agent owner, fixed calendar expiry and exit criterion. Expiry
never blocks or re-admits; protected checks are excluded only under a
reserved-class hold; a new test blocks only its own PR until the consumer's
main-run proof. Change and review any exclusion in the owning consumer;
loop-zero has no advisory stages. Prefer fixing; one logged retry in place is
allowed, no suite-wide skip or hidden xfail.

**Liveness.** Owners route and workers build: an owner commissions build work to
a worker and never builds in a turn that answers a message. Measure progress by
work output on the commissioned deliverable (a commit, push, PR or checks
started on a new head), never by turns. Acknowledgements, unrelated commits,
check reruns the [receipt reuse rules](../core/CONTRACT.md#check-reuse-and-delivery-priority) do not require, and uncommitted changes older than 60 minutes are
not output; workers commit at every green step and push at least hourly. A
stream holding addressable work is stalled after 60 minutes without output or
after three acknowledgement-only or empty turns in a row: push it once with an
instruction to act in that turn, naming the next command. A campaign owner
holding addressable build work never ends a turn with only an acknowledgement.
At every wake and handover, parents check live delegates for waiting questions or
permission prompts by reading their thread state, including the pending-request count and
pending-request list. Answer within authority or route the request to the owner.

**Event-triggered wake.** Check progress when evidence shows something is
off, not on a clock. Host policy decides which signals wake an owner (for
example startable work waiting while slots are free, a Liveness stall, a
merge-authorised green PR left unqueued, or a merge-authority hold whose end
condition is met) and wakes only the owner concerned, with that one finding.
An unchanged finding does not wake again, and an unreadable source is unknown.
On a wake, read every claim the decision rests on from the remote: reported work
shortages, "in the queue", the change class from the PR file list. A stale draft is not
ownership. Record one entry: the outcome and its current condition, the
obstacle, the next step with its predicted effect and check time, and at that
check the actual result. Judge these wakes by changed next actions and shortened
blockages; each adjustment goes to the weekly retro for its verdict.

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
3. A merge-authority hold that remains after its end condition is met or
   restricts a commissioned outcome for 24 hours; a retro never lifts it.
4. The human owner, or the decisions owner for them, corrects an evidenced
   wrong claim, unauthorised restriction, lost ownership or failed
   continuation. Go-aheads, priorities and plain questions do not count.
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

Close the loop on a schedule that host policy sets: the weekly
[retro skill](../core/skills/retro/SKILL.md) rules on every retro action whose
check date has arrived or passed, collected from the owning issues and PRs,
including actions that never shipped. It also pushes the owner of any trigger event still missing
its retro. An action more than a day overdue counts as an occurrence under
trigger 6.

**Goal first.** Each owner holds its commissioned outcome (for example
"ordinary changes reach users") and treats its plan as replaceable; finishing
a step, package or subtask never redefines success. Within the human owner's
explicit priorities, work on whatever most limits that outcome now: implementation,
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
(more than one core for more than a minute, including builds, type checks,
formatters and parallel tests) acquires one only while it runs.
Host slot counts, load thresholds and quota guards belong in host policy.
Tune dispatch from useful output, startable work supply, review/test wait, rework
and merge service; reduce new commissions when downstream work piles up.
Commission whole startable outcomes; do not slice an outcome to occupy an idle
worker, and sequence tasks likely to edit the same files.
Give every addressable issue a next-action owner or a named dependency. Work on
the current bottleneck gets dispatch and shared test slots first; other running
work finishes or checkpoints its current heavy command and releases its slot.

**Schedules.** A recurring schedule belongs to its role owner (the coordinator for delivery),
never to a worker or one-day owner; workers get one-shot wakeups only. Each run
of a recurring task first checks that its previous run's PR merged and adopts
that PR if not. An owner with backlog always keeps an enabled wake: nobody
pauses its scheduled run to save wakes while its backlog is non-empty, and each
run dispatches workers from the backlog, not fixed triggers.

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

Gemini 4 Neon at High is a peer option to Astra and Fable for critical
analysis and an option for substantial coding of the kind suited to Opus.
Use `root_cause_analysis` for a failure whose cause is still unknown. Prefer
Neon when the user prompts Antigravity directly, and use the profile default
for agent-driven diagnosis. Google supports directly prompted Antigravity
sessions in ACP editors such as T3 Code. With a consumer Antigravity login,
select Neon only for that direct use, including when supervised delegation
would be possible in the runtime.
[Google's Antigravity delegation guidance](https://discuss.ai.google.dev/t/tos-clarification-is-human-supervised-codex-to-antigravity-acp-delegation-permitted/200697)

## Profiles are starting points

Profile names describe task shape, not measured cost or speed. Preserve owner
assignments; reassess with evidence and owner direction. Weigh uncertainty,
consequences and verification quality.

Reassess when scope grows or results are unreliable. Fix environment failures
before escalating models; escalate substantive difficulty directly to a suitable
profile. Avoid blind retries or repeating successful work for reassurance.

## Author lineage and independent review

Follow the contract's [review budget](../core/CONTRACT.md#review-budget),
including credit for the writer of a supplied patch.

## Briefs and ownership

Give each writing delegate its own task worktree and a self-contained brief.
`loopzero start <task-slug>` creates it under the primary checkout's `.worktrees/`.
Run it from the primary checkout or a separate launcher worktree, outside any
existing task worktree. For a protected production checkout use a launcher-made
worktree instead ([step 1](../core/CONTRACT.md#the-six-steps)).

The first shell command enters that task tree; repeat `cd <own-task-tree> &&`
at the start of every shell command, including reads and background launches.
Set the launcher's working directory too when supported; never rely on the
inherited project root.

```text
cd <own-task-tree>
Objective: <one bounded outcome>
Context: <source text, decisions, files, why any approach the delegate might retry was ruled out (with the evidence), and requester's wording/link authorizing the finish point; sandboxed delegates usually lack network/gh>
Allowed paths: <owned scope>
Acceptance evidence: <observable proof for each criterion>
Model/effort: <resolved from the task repository's models.toml>
Effort policy: Children inherit this policy; return to the parent before raising effort or launching a stronger model.
Launch and waiting: Children inherit this rule: never approval-required; follow host rules for launch checks and blocking waits. Every ended turn wakes the parent: end it only with a final result, a blocker or a question, never with a progress note while your own job runs.
Output: <host output limits. Select GitHub REST fields with `--jq`.>
Shell commands: Keep cleanup/removal and commit operations separate and simple; do not hide commands in scripts to evade approval review.
Verification: <focused owning tests and coverage; available proof methods and their task-scoped authorization within runtime permissions>
Integration executor: <named executor for returned heads and one owner of any readiness/merge waiter; parent retains acceptance responsibility>
Parent verification: <broad suites and loopzero check; delegates do not repeat these or start reviewers>
Follow-ups: <ordered list of 1–2 already commissioned, bounded items, or none; assign only to delegates that can both write Git and post to GitHub themselves>
Return: <authorized finish: evidence, readiness, or merge; deployment needs its own commission>. Continue until that boundary or a named blocker needs the parent; then follow only the preassigned list, otherwise return. Delegates that cannot do both: write Git and post to GitHub themselves (including sandboxed delegates, delegates without GitHub access, or delegates unable to write the shared Git directory) get no preassigned follow-ups and return after one item via their final Report. A progress summary is not completion.
Report: Final message to the parent, which posts it on the owning PR or issue (post directly only when the delegate has GitHub access): changed files, evidence, check results, deviations, open questions and next action. Send no mid-task messages to the parent; report a blocker at once in this final message (see Worker).
```

Check the repository's queue rules before setting a readiness-only finish point.
If leaving draft auto-queues the PR and the brief must stop before admission,
keep it in draft and hand back the check and review evidence.

Follow [Worker](#worker) for blockers. Continue independent work while a
decision is pending; report the blocker and next owner. Silence grants no approval.

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

Run the delegate in the [Long-running jobs](#long-running-jobs) unit:

```sh
systemd-run --user --unit=NAME --working-directory=/abs/worktree \
  --property=RuntimeMaxSec=LIMIT --collect bash -c '
  codex exec --model MODEL -c model_reasoning_effort=EFFORT \
    --sandbox workspace-write --cd /abs/worktree \
    --output-last-message /abs/final "$(cat /abs/brief)" \
    </dev/null >/abs/LOG 2>&1; echo $? >/abs/LOG.exit'
```

Wait and cancel as in [Long-running jobs](#long-running-jobs), then read
`/abs/final` once if it exists.

App-orchestrated delegates (for example T3 `delegate_task`) launched in an
approval-requiring mode stall unattended at their first tool call, while task
status still reads running. For an unattended read-only consult, choose the
narrowest mode within your own authority that runs reads without prompts (T3:
`runtimeMode: auto`) and state "read-only, do not edit" in the brief. That is
an instruction boundary, not an OS sandbox; when writes must be impossible,
use a CLI delegate with `--sandbox read-only`.

Launch independent tasks in small concurrent batches within host guards. Retain
each delegate task ID and verify the batch concurrently.

Running or working status does not prove progress. After a launch, and again
when a delegate stops producing output, read its activity: a completed tool call,
no pending request (T3 `pendingRequestCount` 0) and its terminal task state,
including provider/capacity failures. Answer a request only within your
authority; otherwise cancel, relaunch correctly or report the block in that
turn. Inspect what ran before resuming interrupted work. A cancelled or declined
tool call counts as a human stop only when the run is marked interrupted or the
human owner says so; otherwise retry it once. Held automation messages release
on the next successful run; for held human messages and runs after an explicit
stop (T3 `queueHeld=true`), save the full text, cancel with `t3_queue cancel`,
combine still-needed messages into one fresh resend and confirm it arrived. For
a terminal delegated task, launch a new task with the prior brief and result.
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
in [Goal first](#team-practices). A repair brief lists every unresolved review
finding and deterministic test failure on the failed head; an unrelated repair
does not fix one.

Consult and artifact reviews outside a PR use the same budget: one primary review, then at
most one delta per artifact. Reviewers classify each finding as blocking (it can cause an
unsafe or unintended effect, a missed stop or a false admission) or follow-up. A blocking
finding states the concrete failure scenario, its consequence and the smallest correction;
a patch is optional. Without a failure scenario it is a follow-up. The parent checks its
premise against the actual system (independent reviewers can share a wrong premise); the
worker integrates the correction and proves the behavior, without reopening review.
Follow-ups wait.
Workers change only what the brief names and keep regenerated evidence inside its
declared allowlist; a scope-only or metadata finding never earns another round.

Read-only work may share stable source. Sequence overlapping writers or
integrate deliberately; never run them in one checkout. Do not pre-clear file
overlap by message. Ask once, on the PR, only when both change the same function.
Respect concurrency limits and avoid competing suites whose temporary-directory cleanup
can interfere. Heavy commands take a test slot as in [Team practices](#team-practices).
When the host is saturated, shed out-of-slot CPU
commands before shedding agents; preserve their work. Grant only a needed cache
directory (`--add-dir ~/.cache/uv`);
shared `/tmp` caches can exhaust host inodes. If a linked worktree's shared Git
directory is outside the writable sandbox, leave commits to the parent. Every
dependency names its producer and the producer's next action.

### Stacked delivery

Use a stack only if the repository supports it and has a supported trigger for
required target-branch CI after retargeting. Workflows can exclude the producer
branch and ignore both base edits and leaving draft. If either prerequisite
is missing, follow the repository's dependency route and publish against the
configured integration base.

Agree the configuration with the coordinator: copy the trusted
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
the coordinator must verify the copy and its one-field difference. Keep the
producer as the base through primary review and any delta covering a merge
of the producer. Retarget to main before the producer's `loopzero merge`
deletes its branch. After retargeting, take producer updates by merging main. After
the producer lands, confirm the consumer is open against main, return to the
canonical configuration, and use the supported CI trigger. Verify that all
required checks pass on the current head against main before readiness.
Follow the [receipt reuse rules](../core/CONTRACT.md#check-reuse-and-delivery-priority)
and [review budget](../core/CONTRACT.md#review-budget).

### Integration and acceptance

Nested background results may reach the top-level session instead of their
owner: run nested reviewers in the foreground or forward results promptly.

The parent owns integration and acceptance, and may name an integration executor
in the brief: inspect returned diffs and evidence; worker success claims are not
proof. Route returned heads to that executor. Run decisive tests unavailable to
the delegate before review, and required broad verification and `loopzero check`
under the [receipt reuse rules](../core/CONTRACT.md#check-reuse-and-delivery-priority). If declared checks do not isolate host tools, run a
focused restricted-PATH probe and report the limitation. Only a sandboxed
`loopzero check` receipt satisfies `loopzero ready`; continue under the
[contract](../core/CONTRACT.md).

## Configuration and upgrades

Follow [T3 upstream review](T3-UPSTREAM.md) for T3 updates; discovery does not
authorize an upgrade or change delivery policy.

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
