# The loop-zero contract

One task, one branch, one draft PR, one review, one merge. Git, GitHub and CI
hold all state. Nothing is recorded anywhere else.

## The six steps

1. `loopzero start <task-slug>` — create a worktree and branch off the current
   base. Write `.loopzero/task.md` from the [handoff template](HANDOFF.md):
   context, problem, goal, acceptance, base SHA.
2. Implement, then `loopzero check` — run the commands declared in
   `workflow.toml` `[checks].commands` inside the sandbox described below.
   The exit code is the verdict. Nothing is signed or archived.
3. `loopzero pr` — push and open a **draft** PR. The body is
   `.loopzero/task.md` plus a generated checks block within Validation. Later
   `pr` calls sync task narrative but preserve live Validation, Review and extra
   sections; `check` refreshes only the checks block. A draft never needs a clean review.
4. `loopzero review` — run one independent model review on the exact head.
   Findings are posted as one GitHub PR review with inline comments. It exits 0
   on `approve` and 5 on `request_changes`.
5. `loopzero ready` — compute readiness (below). If ready, mark the PR ready
   for review. If a draft is blocked only by missing or skipped required
   checks, mark it ready to trigger CI and exit 3. With `--wait[=SECONDS]`
   (default 1800) it polls the required checks of that unchanged head instead:
   exit 0 ready, 1 blocked, 3 timed out. If required checks remain missing after
   two minutes with no visible pending checks, inspect workflow scheduling,
   triggers and permissions for that head, then retry after correcting the cause. Do not gate on `gh pr checks --watch`: it ignores checks that do
   not exist yet.
6. `loopzero merge` — recompute readiness, merge with the configured strategy,
   verify the merge SHA, delete the branch and the worktree. Under a merge
   queue the first run enqueues; `--wait[=SECONDS]` follows the queue to the
   landed SHA and cleans up in the same run.

`loopzero status` prints where a branch is in this sequence, derived from Git
and GitHub only.

End each delivery response with its outcome: the authorized scope is complete,
or it is blocked (name the exact dependency or decision), or it is waiting
(name the event, the next action, and whether it resumes on its own or needs
the requester).

## Blockers

A PR is ready when none of these hold:

- A required check exited nonzero on the current head.
- An open review thread on the PR carries an unresolved `critical` or
  `important` finding.
- The head moved after the last review.
- GitHub reports the branch as `BEHIND`, unless configured Mergify mode has
  verified source-preserving integration. Mergify validates a separate integration
  candidate against the base, so base movement alone does not invalidate review
  of the authored head. Actual merge conflicts remain blockers; source edits,
  including conflict fixes, require renewed review and checks.
- A CI check named in `[checks].required_ci` is missing or not green on the
  exact head.

Resolve a blocker by pushing a fix or resolving the thread with a reason
(`loopzero resolve <id> "<reason>"` replies and resolves one finding; it never
resolves in bulk or without a reply). There
is no waiver file and no override flag. Hosted review eligibility uses the exact authored SHA, with publishers configured in
trusted base `workflow.toml`; pending or dismissed review evidence is ineligible.
Local readiness additionally requires its sandboxed check receipt and source CI.

Readiness is a checklist derived from
Git and GitHub, not a tamper-proof boundary: anyone with write access can edit
or resolve threads; branch protection and CI remain the enforced gates.

## Findings

- Findings live only in PR review threads. Each blocking finding is marked with
  `<!-- loopzero:finding v=1 severity=critical head=<sha> id=<8-hex> -->` (or `important`).
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
- Every committed head change needs renewed review within this budget, because
  readiness requires the reviewed head to be the PR head. Batch mechanical fixes
  (format, lint, rename) into the commit the delta review will read.
- A fresh lineage is not a way to keep patching: repeated findings of one defect
  class mean the class was not audited; see `resolve-findings`.
- A reviewer run that yields no verdict (missing tool, authentication failure,
  unparseable output) does not consume the budget; `loopzero review` moves to
  the next configured family and exits nonzero with each reason if all fail.
A review file not produced by a runner is never a review; if no independent reviewer can run, the delivery waits for the owner.

## Sandbox rules for checks

Every command in `[checks].commands` runs with:

- The source tree read-only, writes to scratch, no write access to `.git`.
- An isolated `HOME`; only `[checks].ro_paths` from your home are visible.
- No network unless `[checks].network = true`.
- Environment reduced to the allowlist (`PATH HOME LANG LC_ALL TERM` default).

If `bwrap` cannot run, `loopzero check` fails with `SandboxUnavailable`; it
never runs checks unsandboxed.

Reviews also require `bwrap` and never fall back to the host. They receive a
read-only worktree and Git directories, a private HOME containing only the
selected reviewer's copied credential file, explicitly configured reviewer
runtime paths, and network access. The sandbox bounds readable data; it cannot
prevent the reviewer from sending readable data over the required network.

## Failure

Every command fails closed and loud: a missing tool or a nonzero exit prints
the command and the tail of its output and stops. Nothing retries on its own.

Keep that property when chaining commands: never pipe a `loopzero` command
into `tail`, `grep` or `head` (the filter's exit status replaces the verdict);
redirect output to a file, chain with `&&` under `set -o pipefail`, and treat
exit 3 (waiting) and 5 (changes requested) as outcomes, not crashes. The last
line of output states the verdict. Report a `loopzero` outcome with its exit code
and that last line; never infer queue or merge state from an earlier progress line.

A successful `loopzero merge` deletes its worktree. Run it from the repository
root in a subshell, `(cd <worktree> && loopzero merge --wait)`: an agent shell
left standing in the deleted directory fails its next command with a `getcwd`
error that looks like a failed merge.

Wait with `loopzero ready --wait` and `loopzero merge --wait`, never with a
hand-written `gh` loop. Any other poll needs a deadline, visible progress,
stderr kept, and a stop on command errors: a silent loop looks like running CI.
Run one waiter per PR. Do not poll with `gh pr view`, `gh pr checks`, or
`gh pr list`; their hidden GraphQL requests share the account's quota across
sessions. For one-off PR reads or comments, use the relevant REST `gh api`
endpoint. A quota error stops the command until a verified reset or
`Retry-After`; REST `/rate_limit` does not establish GraphQL capacity.
