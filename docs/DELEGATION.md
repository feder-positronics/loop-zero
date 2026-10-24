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
with their original task reference, and makes no new decisions. After a finished
delivery, roll over before monitoring or unrelated follow-ups. Do this
yourself; do not ask the requester to start it. A bare "continue" means
continue there.

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

The contract's [review budget](../core/CONTRACT.md#review-budget) owns authorship
and reviewer selection. Keep one writing family by default; the other can advise
read-only. Before committing, record every actual writer in truthful
`Co-Authored-By` trailers. The parent committing a delegate's patch does not
replace that author.

Without the contract's
[owner-approved exception](../core/CONTRACT.md#owner-approved-contributor-review-exception),
retain all writers' attribution through amendments and squashes. If mixed
authorship leaves no independent configured reviewer, review fails closed:
wait for the owner. Do not re-author work or drop trailers to regain eligibility.
Read the exception before mixing writing families. Formal delivery review is
`loopzero review`.

## Briefs and ownership

Give each writing delegate its own `loopzero start` worktree and a
self-contained brief:

```text
Objective: <one bounded outcome>
Context: <source text, decisions, files; sandboxed delegates usually lack network/gh>
Allowed paths: <owned scope>
Acceptance evidence: <observable proof for each criterion>
Model/effort: <resolved from the task repository's models.toml>
Effort policy: Children inherit this policy; return to the parent before raising effort or launching a stronger model.
Launch policy: Children inherit this rule: never approval-required; auto plus "read-only, do not edit" for read-only work, otherwise the parent's mode; after launch, verify pendingRequestCount 0 and a completed tool call.
Verification: <focused owning tests and applicable coverage, runnable in the sandbox>
Parent verification: <broad suites and loopzero check; delegates do not repeat these or start reviewers>
Return: Continue until acceptance evidence exists or a named blocker needs the parent; a progress summary is not completion.
Report: <changed files, evidence, check results, deviations, open questions>
```

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

Running or working status does not prove progress. Before ending the turn
after a launch, read the delegate's activity: a completed tool call and no
pending approval (T3 `t3_thread_read`: `pendingRequestCount` 0). Answer an
approval only within your authority; otherwise cancel, relaunch correctly or
report the block. Before yielding on any delegate or long command, arrange a
wakeup whose timeout covers the awaited event: a completion notice plus a
fallback timer, or a bounded blocking wait. Before arming or rearming a wait,
reconcile the awaited task's existing terminal result and current remote
state; after a superseding message or restart, check again before concluding
"still waiting".

Split oversized scope in the plan rather than imposing brief line limits.
Resume unfinished delegate sessions instead of restarting. After two repair
rounds on one defect class, reassess rather than dispatching another isolated fix.

Read-only work may share stable source. Sequence overlapping writers or integrate
deliberately; never run them in one checkout. Respect concurrency limits and avoid
competing suites whose temporary-directory cleanup can interfere. Grant only a
needed cache directory (`--add-dir ~/.cache/uv`); shared `/tmp` caches can exhaust
host inodes. If a linked worktree's shared Git directory is outside the writable
sandbox, leave commits to the parent. Retarget stacked PRs before deleting their base.

Nested background results may reach the top-level session instead of their
owner: run nested reviewers in the foreground or forward results promptly.

The parent owns integration and acceptance: inspect returned diffs and evidence;
worker success claims are not proof. Run decisive tests unavailable to the
delegate before review, and required broad verification and `loopzero check`
once per delivery head. If declared checks do not isolate host tools, run a
focused restricted-PATH probe and report the limitation. Only sandboxed
`loopzero check` counts as delivery proof; continue under the
[contract](../core/CONTRACT.md).

## Configuration and upgrades

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
