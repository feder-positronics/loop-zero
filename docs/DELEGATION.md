# Model selection and delegation

Delegate independent, bounded work whose result can be checked. Prefer the
least expensive reliable model; keep small or tightly coupled work inline
when briefing and integration cost more. No delegation quota, line threshold
or exception record is required.

## Coordinator rollover

At a delivery milestone or roughly 300k context tokens, launch a successor
with a short handoff, transfer recurring tasks and pending wakeups bound to
this session, post its link and stop. The handoff is enough for the next
action: live child IDs, pending results or questions, still-binding decisions
with their sources and the current reply target; link history instead of
copying it. Reconcile that work with the successor before stopping. A launch
notice alone is not a task transfer. A changed decision names the instruction
it supersedes. After transfer, the retired session forwards late results once,
with their original task reference, and makes no new decisions; then it retires
its session so no later send or schedule can revive it. After a finished
delivery, roll over before monitoring or unrelated follow-ups. Do this
yourself; do not ask the requester to start it. A bare "continue" means
continue there. Workers report on the owning PR or issue, directly or through
the parent; the coordinator reconstructs delivery from live remote state rather
than inbound messages.

**Provider-portable handoff.** If an owner's provider is exhausted or unavailable,
use a fresh successor on another provider or model only within an explicit
standing route; preserve commissioned choices and runtime permissions. Otherwise
report the unavailable route and wait for owner direction. Confirm the old
writer and its tool tree have stopped or are fenced before the successor acts.
Keep the existing handoff file current after consequential decisions, so recovery
never depends on a final summary from the failed session. Include the owning
issue/PR and evidence links; current authority, holds and decision sources;
worktree path, branch, head and dirty state; checks and interrupted tool outcomes;
live child/task IDs, pending results or decisions, schedules and wakeups; current
reply target and the next authorized action. State lives in Git/GitHub plus this
handoff, not in provider memory or a provider-specific resume token.
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
A schedule firing or a relayed approval grants no new authority.

- **Coordinator:** owns one repository's delivery, main health and shared
  delivery infrastructure; drains ready backlog by tier, routes incidents and
  dispatches workers and reviews. May delegate routine dispatch and integration
  to named executors; it keeps main health, merge freezes and holds, shared
  delivery infrastructure and production observation. Reports to the decisions
  owner.
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
  or one named blocker.
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
mid-task only for a blocker. Monitors and watchdogs keep sending one packet or
nudge per new finding. Count productive work by verified activity (completed tool
calls or output on the deliverable), including nested reviewers; report test-slot,
review, queue and hold waiters separately, excluding them from productive counts.

**State broadcasts.** Announce a merge freeze, production window or hold by its
end condition ("until the coordinator broadcasts LIFT"), never as a clock
window. Send every freeze, lift, hold and release to every active owner,
resolved when sending, not only to owners in the current conversation. Keep the
current state on one line of the live decision record ("Merge freeze: …"). Every
owner checks that line before holding a merge; a merge freeze ends only on the
coordinator's LIFT broadcast. A merge freeze pauses merging only, never
building, checks or review.

**Liveness.** Owners route and workers build: an owner commissions build work to
a worker and never builds in a turn that answers a message. Measure progress by
work output on the commissioned deliverable (a commit, push, PR or checks
started on a new head), never by turns. Acknowledgements, unrelated commits,
re-runs of unchanged checks and uncommitted changes older than 60 minutes are
not output; workers commit at every green step and push at least hourly. A
stream holding addressable work is stalled after 60 minutes without output or
after three acknowledgement-only or empty turns in a row: push it once with an
instruction to act in that turn, naming the next command. A campaign owner
holding addressable build work never ends a turn with only an acknowledgement.

**Priority tiers.** Tier 0 is these team mechanics, above all delivery. Tier 1
is big features and framework improvements; tier 2 is QA fixes, flakes and
smaller tasks. The decisions owner records assignments in the live decision
record. Honor explicit owner priorities; tier 1 gets dispatch and shared test
slots first.
Bound dispatch by shared resource slots and host guards, never by agent count.
Implementation work does not require or reserve a test slot; a heavy command
(more than one core for more than a minute) acquires one only while it runs.
Host slot counts, load thresholds and quota guards belong in host policy.
Tune dispatch from useful output, eligible work supply, review/test wait, rework
and merge service; reduce new commissions when downstream work piles up.
Give every addressable issue a next-action owner or a named dependency. A running
tier-2 worker finishes or checkpoints its current heavy command and releases that
test slot when tier 1 needs it; a queued tier-2 job steps aside for a queued tier-1 job.

**Rollover.** [Coordinator rollover](#coordinator-rollover) applies to every
owner role. Before acting, the successor revalidates recipients, schedules and
carried decisions against live sources and reports what it cannot confirm as a
blocker. Peers resolve the current owner when sending, so a rollover sends no
routing notices; state broadcasts still reach every owner. The successor retires
the predecessor in the turn it acknowledges or, while the predecessor still
parents live delegates, when the last one finishes.

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
Context: <source text, decisions, files; sandboxed delegates usually lack network/gh>
Allowed paths: <owned scope>
Acceptance evidence: <observable proof for each criterion>
Model/effort: <resolved from the task repository's models.toml>
Effort policy: Children inherit this policy; return to the parent before raising effort or launching a stronger model.
Launch policy: Children inherit this rule: never approval-required; auto plus "read-only, do not edit" for read-only work, otherwise the parent's mode; after launch, verify pendingRequestCount 0 and a completed tool call.
Shell commands: Keep cleanup/removal and commit operations separate and simple; do not hide commands in scripts to evade approval review.
Verification: <focused owning tests and applicable coverage, runnable in the sandbox>
Integration executor: <named executor for returned heads; parent retains acceptance responsibility>
Parent verification: <broad suites and loopzero check; delegates do not repeat these or start reviewers>
Follow-ups: <ordered list of 1–2 already commissioned, bounded items, or none; assign only to delegates that can both write Git and post to GitHub themselves>
Return: Continue until acceptance evidence exists or a named blocker needs the parent; then follow only the preassigned list, otherwise return. Delegates that cannot do both: write Git and post to GitHub themselves (including sandboxed delegates, delegates without GitHub access, or delegates unable to write the shared Git directory) get no preassigned follow-ups and return after one item via their final Report. A progress summary is not completion.
Report: Final message to the parent, which posts it on the owning PR or issue (post directly only when the delegate has GitHub access): changed files, evidence, check results, deviations, open questions and next action. Message the parent mid-task only for a blocker.
```

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

Record `$!` and use `kill -0 <pid>`, never `pgrep -f`: pattern matching also
finds the waiting shell and can wait forever. Prefer completion notifications
to reading output files. Wait on long commands with blocking calls (Codex:
yield at least 30 seconds), not second-by-second polling.

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
before resuming interrupted work. For a terminal delegated task, launch a new
task with the prior brief and result rather than queueing another message to it.
Before yielding on any delegate or long command, arrange a
wakeup whose timeout covers the awaited event: a completion notice plus a
fallback timer, or a bounded blocking wait. Before arming or rearming a wait,
reconcile the awaited task's existing terminal result and current remote
state; after a superseding message or restart, check again before concluding
"still waiting".

Split oversized scope in the plan rather than imposing brief line limits. Resume
unfinished delegate sessions instead of restarting. After two repair rounds on
one defect class, reassess rather than dispatching another isolated fix. A
repair brief lists every unresolved review finding and deterministic test
failure on the failed head; an unrelated repair does not fix one.

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
once per delivery head. If declared checks do not isolate host tools, run a
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
