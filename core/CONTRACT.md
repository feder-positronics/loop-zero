# The loop-zero contract

One task, one branch, one draft PR, one review, one merge. Git, GitHub and CI
hold all state. Nothing is recorded anywhere else.

## The six steps

1. `loopzero start <task-slug>` — create a worktree and branch off the current
   base. Write `.loopzero/task.md` from the [handoff template](HANDOFF.md):
   objective, acceptance, base SHA.
2. Implement, then `loopzero check` — run the commands declared in
   `workflow.toml` `[checks].commands` inside the sandbox described below.
   The exit code is the verdict. Nothing is signed or archived.
3. `loopzero pr` — push and open a **draft** PR. The body is
   `.loopzero/task.md` plus the check summary. A draft never needs a clean
   review.
4. `loopzero review` — run one independent model review on the exact head.
   Findings are posted as one GitHub PR review with inline comments.
5. `loopzero ready` — compute readiness (below). If ready, mark the PR ready
   for review.
   If a draft is blocked only by missing or skipped required checks, mark it ready to trigger CI and exit 3 while waiting for those checks.
6. `loopzero merge` — recompute readiness, merge with the configured strategy,
   verify the merge SHA, delete the branch and the worktree.

`loopzero status` prints where a branch is in this sequence, derived from Git
and GitHub only.

## Blockers

A PR is ready when none of these hold:

- A required check exited nonzero on the current head.
- An open review thread on the PR carries an unresolved `critical` or
  `important` finding.
- The head moved after the last review.
- A CI check named in `[checks].required_ci` is missing or not green on the
  exact head.

Resolve a blocker by pushing a fix or resolving the thread with a reason. There
is no waiver file and no override flag. Readiness is a checklist derived from
Git and GitHub, not a tamper-proof boundary: anyone with write access can edit
or resolve threads; branch protection and CI remain the enforced gates.

## Findings

- Findings live only in PR review threads. Each blocking finding is marked with
  `<!-- loopzero:finding severity=critical head=<sha> -->` (or `important`).
- Severities: `critical` (wrong or unsafe; must fix), `important` (defect
  that ships a bug or breaks acceptance; must fix), `suggestion` (never
  counted, never blocks).
- Findings expire at merge. Do not copy them into files, issues or backlogs.

## Review budget

- One primary review per head lineage, by a model family different from the
  author's when the author is known.
- At most one delta review after fixes; it reads only the diff since the
  reviewed commit and inherits the primary's open threads.
- Fixes after the delta review require a fresh lineage: new commits, new
  primary review.
- Mechanical fixes (format, lint, rename) need checks rerun, not a review.
- A reviewer run that yields no verdict (missing tool, authentication failure,
  unparseable output) does not consume the budget; `loopzero review` moves to
  the next configured family and exits nonzero with each reason if all fail.

## Sandbox rules for checks

Every command in `[checks].commands` runs with:

- The source tree read-only, writes to scratch, no write access to `.git`.
- An isolated `HOME`; only `[checks].ro_paths` from your home are visible.
- No network unless `[checks].network = true`.
- Environment reduced to the allowlist (`PATH HOME LANG LC_ALL TERM` default).

If `bwrap` cannot run, `loopzero check` fails with `SandboxUnavailable`; it
never runs checks unsandboxed.

## Failure

Every command fails closed and loud: a missing tool or a nonzero exit prints
the command and the tail of its output and stops. Nothing retries on its own.
