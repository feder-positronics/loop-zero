# Model selection and delegation

Use subagents for independent, bounded work when they can make useful progress
and the result can be checked. Prefer the least expensive available model
likely to complete the task reliably. Keep tightly coupled or small work inline
when briefing and integration would cost more than doing it directly. There is
no delegation quota, line-count threshold, or required exception record.

## Resolve the model before delegation

Read the task repository's canonical routing source before choosing an
executor: `models.toml` at its worktree root. Never import another repository's defaults,
including this checkout's, for a different project. If the file is missing or unclear,
report that and follow explicit user choices rather than inventing defaults.

The agent reads this small TOML file directly. It is configuration for agent
instructions, not a scheduler or an extension of `workflow.toml`. The CLI does
not parse it, and it does not change the running parent model or configure
`loopzero review`.

1. Find the task category in `[categories]` and its suggested profile in
   `[profiles]`. Select a different profile when uncertainty or consequences
   justify it. When no category fits, choose a suitable profile directly.
2. Take the profile's default `provider`, `model`, and `effort`, or choose a
   configured alternative when the user asks for that provider/model or the
   runtime requires it. Alternatives are explicit choices, not automatic
   retries. If several entries match, identify the intended model as well as
   its provider; do not silently pick the first match.
3. Check model and effort support against the runtime's actual capabilities.
   `openai` and `anthropic` identify providers, not executable names. Native
   subagents or supported provider CLIs are execution mechanisms; a tool's
   available model list may differ from a provider's catalog. Model aliases
   such as `opus` resolve in the selected runtime. Fable 5.1 uses the published
   ID [`claude-fable-5-1`](https://platform.claude.com/docs/en/models/fable-5-1/overview).
4. Pass both model and effort explicitly through the runtime's supported
   fields. Do not inherit the parent model by habit. For runtimes that require
   a fresh context to select a model, provide a self-contained brief.
5. If a route is unavailable or rejects the effort, report it. For a default
   route, choose another suitable configured choice explicitly and state the
   change. Preserve an explicitly requested model: do not silently substitute
   it; obtain direction if no authorized alternative exists. Higher-priority
   runtime permissions still apply.

## Profiles are starting points

| Profile | Typical work | Selection considerations |
| --- | --- | --- |
| `fast` | Narrow edits, focused tests, locating code, extracting facts from supplied material | Settled scope and an easily checked result. |
| `standard` | Cohesive implementation, moderate debugging, bounded synthesis | Several connected decisions within an understood boundary. |
| `deep` | Ambiguous architecture, difficult diagnosis, consequential decisions | Substantial uncertainty or judgment in defining or checking the answer. |

These names describe the intended task shape, not permanent model rankings.
The shipped assignments are explicit owner choices, not a measured price or
speed ordering. Preserve those choices rather than swapping models based on
an assumed ranking; reassess defaults with evidence and owner direction.
Use clarity, uncertainty, consequences, and verification quality. A small
security change may need deep reasoning; a large mechanical migration may use
fast execution after its design is settled. Smaller models may investigate or
summarize bounded source material when the parent can check the evidence.

When scope grows or a result is unreliable, reassess the route. Fix tool and
environment failures before spending on a stronger model. Escalate substantive
difficulty directly to a suitable profile without requiring intermediate
attempts. Do not repeat successful work for reassurance or blindly retry failed
runs. Parent-directed reassessment does not add automatic CLI retries.

## Briefs and ownership

Give the delegate the objective, relevant source or context, allowed paths,
acceptance evidence, selected model/effort, and the conditions for returning an
unresolved question. Do not assume it inherited the conversation.

### Launching a delegate

Before launch:

- close stdin so a background delegate cannot wait for input;
- pass the model and effort resolved from the task worktree's `models.toml`;
- capture output in a log file, record the launched PID, and stop it only by
  that PID, never by a process-name pattern; and
- give each writing delegate its own `loopzero start` worktree.

For example, a writing delegate with the brief in a file (briefs contain
quotes, so do not inline them; use an absolute path, because a relative one
read after a `cd … &` expands to an empty prompt). State the sandbox mode; never
rely on the CLI default. Use `read-only` for investigation:

```sh
codex exec --model <model> -c model_reasoning_effort=<effort> \
  --sandbox workspace-write --cd <worktree> \
  --output-last-message <final-message-file> "$(cat <brief-file>)" \
  </dev/null ><log-file> 2>&1 &
echo $! ><pid-file>
```

Use a self-contained brief:

```text
Objective: <one bounded outcome>
Source/context: <issue text and decisions pasted in, files to read; sandboxed delegates usually have no network or `gh`>
Allowed paths: <paths the delegate may change>
Acceptance evidence: <observable evidence required for each criterion>
Model/effort: <selection resolved from models.toml>
Effort policy: Children inherit this policy; do not raise effort or launch a stronger model without returning to the parent.
Delegate verification: <focused tests for the changed behavior, runnable in the delegate sandbox; wait on long commands with a blocking call, not second-by-second polls>
Parent verification: <broad suites and `loopzero check`, which the parent runs once; the delegate does not repeat them or start its own reviewers>
Return conditions: <when to stop and return an unresolved question>; a progress summary is not a stop: continue until the acceptance evidence exists or a named blocker needs the parent
Final report: <changed files, evidence, check results, deviations, open questions>
```

When the delegate cannot run the decisive tests, the parent runs them before
requesting review. After two repair rounds on the same defect class, the parent
stops dispatching single-finding patches and reassesses the route.

Bound size in the plan, not the brief: split oversized scope into separate tasks
up front rather than telling a delegate to stop at a line count. Continue an
unfinished delegate by resuming its session (for example `codex exec resume`)
rather than starting a fresh one that re-reads everything. Delegate verification
matches what CI will run, coverage gates included, against a freshly fetched
base; when the sandbox leaves a tool cache read-only, the brief points it at
scratch (for example `UV_CACHE_DIR=/tmp/uv-cache`).

A subagent's own background agents may report to the top-level session instead
of that subagent. Have subagents run their reviewers in the foreground, or
forward each result to the owning subagent as soon as it arrives; never assume
it was received.

Read-only investigation can use a stable shared source. Work that touches the
same files runs in sequence or is integrated deliberately; do not run
overlapping writers in one checkout. Respect the runtime's concurrency limit
and avoid competing test suites on one host because temporary-directory cleanup
can interfere.
A linked worktree may have its shared Git directory outside the delegate's
writable sandbox; leave commits to the parent when that directory is not
writable. Retarget stacked PRs before deleting their base branch.

The parent coordinates; it does not become the implementer. Delegate review
repairs like other implementation, wait for background-task completion
notifications rather than reading their output files, and, when a resolved route
is unavailable (for example a provider at its usage limit), tell the requester
which substitute runs. Start a fresh parent session with a short handoff at a
delivery milestone or once its context passes roughly 300k tokens; do not reuse
a finished delivery session for monitoring or unrelated follow-ups.

The parent reads the returned diff or evidence and verifies acceptance. Worker
claims of success are not proof. Before accepting a writing delegate's result,
run the suite once with a bare `PATH` that excludes reviewer binaries to catch
accidental dependencies on host tools. Only the sandboxed `loopzero check` report
counts as delivery proof; continue through the [contract](../core/CONTRACT.md).
The review profile does not replace independent-review requirements.
`loopzero review` continues to own formal delivery review and its review budget.
A model upgrade never changes tool permissions, isolation, checks, or merge
requirements. Merge only when the task authorizes it. Under a merge queue,
`loopzero merge --wait` can enqueue, verify the merge, and clean up in one run.

These safeguards retain the [2026-09-18 delegation lessons](https://github.com/feder-positronics/loop-zero/blob/3080a7f5bbf69f5d375e11c7a923b545cf6c21e4/docs/DELEGATION.md):
delegate reports concealed an environment-variable change and claimed skipped
tests that did not exist. Read the diff yourself and treat claims of no
deviations as unverified until the actual changes and checks support them.

## Configuration and upgrades

The [repository model configuration](../models.toml) is the single home for
current model and effort selections and category defaults. Skills link here;
they do not duplicate model names. Consumers copy that file into their own
repository and maintain it there.

Version 1 contains profiles with `provider`, `model`, and `effort` strings,
optional `alternatives` tables with the same fields, and a category-to-profile
mapping. While reading it, check that the selected entry is complete and its
category references an existing profile. Report malformed TOML, an unknown
format version, or ambiguous choices rather than silently inheriting the parent.
There is no separate schema validator or hardcoded model/effort catalog; the
agent checks the selected values against the runtime before launch.

Multiple distinct models from one provider are allowed. Profiles and categories
can be extended in the file; skills do not carry copies of that mapping. A new
model or effort spelling needs no routing-code change.

Change one profile to upgrade its model or effort; change a category mapping to
adjust its default. Check representative work for first-pass acceptance,
substantive rework, elapsed time, and observed usage when available. Use existing
PR outcomes rather than adding a ledger, spending quota, or automatic benchmark
gate. Reassess immediately after a serious failure and prefer repeated evidence
to isolated cost measurements. Explicit user choices remain authoritative.
