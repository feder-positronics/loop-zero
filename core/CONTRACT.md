# The loop-zero contract

One task, one branch, one draft PR, one review, one merge. Git, GitHub and CI
hold all state. Nothing is recorded anywhere else.

## The six steps

1. `loopzero start <task-slug>` — create a worktree and branch off the current
   base. Write `.loopzero/task.md` from the [handoff template](HANDOFF.md):
   context, problem, goal, acceptance, base SHA.
2. Implement and commit, then `loopzero check` — run the commands declared in
   `workflow.toml` `[checks].commands` inside the sandbox described below.
   The exit code is the verdict. Nothing is signed or archived.
3. `loopzero pr` — push and open a **draft** PR. The body is
   `.loopzero/task.md` plus a generated checks block within Validation. Later
   `pr` calls sync task narrative but preserve live Validation, Review and extra
   sections; `check` refreshes only the checks block. A draft never needs a clean review.
4. `loopzero review` — run one model review on the exact head,
   independent by default; see the owner-approved exception below.
   Findings are posted as one GitHub PR review with inline comments. It exits 0
   on `approve` and 5 on `request_changes`.
5. `loopzero ready` — compute readiness (below). If ready, mark the PR ready
   for review. If a draft is blocked only by missing or skipped required
   checks, mark it ready to trigger CI and exit 3. With `--wait[=SECONDS]`
   (default 1800) it polls the required checks of that unchanged head:
   exit 0 ready, 1 blocked, 3 timed out. After this invocation marks a draft ready,
   it observes readiness again on the same head, within the original wait budget.
   This is best effort: an immediate green observation can precede asynchronous
   CI scheduling; it does not guarantee atomic readiness across the event.
   If required checks remain missing after
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
  `important` finding, unless GitHub marks the thread outdated and its marker
  head differs from the current head.
- The head moved after the last review.
- The PR targets a branch other than configured `repo.base`.
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

- By default, one primary review per head lineage is by a model family that
  did not contribute to the PR. Review selection excludes every recognized
  `Co-Authored-By` family in commits from the configured-base merge-base through
  HEAD, including repairs and all trailers on each commit. Delta reviews use
  the same author lineage; their narrower diff does not restore a contributor's
  independence. Reposting enforces the same trailer-based exclusion. The selector
  cannot infer contributions from prose or determine owner permission. When all
  configured families have recognized lineage trailers, review fails closed:
  there is no native independent route for that mixed-trailer lineage.
- By default, keep one authoring model family per PR, including delegated
  implementation and repairs. The opposite family may give read-only advice and
  formal independent review. Before committing imported work, record every
  actual writing delegate in truthful `Co-Authored-By` trailers; the parent
  committing a patch does not replace its author's family. Preserve every actual
  contributor's attribution when squashing or amending, subject only to the
  explicitly authorized disclosure route below. Missing attribution stays
  unknown: trailers cannot recover an omitted author, and agents must not invent
  one. Outside a valid owner exception, retain every writer's truthful trailers
  and fail closed if no independent configured reviewer remains.
- At most one delta review after fixes; it reads only the diff since the
  reviewed commit. The primary's unresolved blocking threads still count unless
  outdated on a different head.
- Fixes after the delta review exhaust the budget. Audit the defect class, then
  squash/amend so neither reviewed commit remains an ancestor of HEAD; the next
  primary reviews from HEAD's merge-base with the configured base.
- Preserve the final approved head. Do not apply optional suggestions after
  approval. A necessary change requires renewed checks and review: use the
  remaining delta, or, if it is exhausted, audit the defect class and follow the
  squash/amend rule above before a new primary. This is delivery discipline,
  not interception of Git commits.
- Every committed head change needs renewed review within this budget, because
  readiness requires the reviewed head to be the PR head. Batch mechanical fixes
  (format, lint, rename) into the commit the delta review will read.
- A fresh lineage is not a way to keep patching: repeated findings of one defect
  class mean the class was not audited; see `resolve-findings`.
- A reviewer run that yields no verdict (missing tool, authentication failure,
  unparseable output) does not consume the budget; `loopzero review` moves to
  the next configured family and exits nonzero with each reason if all fail.
- A lost review POST reply is reconciled through exact live GitHub evidence under
  one 30-second read-back deadline; failed reads are not retried in that window.
  Confirmation returns the saved runner verdict without another write or model
  run. `review --repost` performs the same reconciliation before budget rejection.
  Missing evidence never causes an automatic POST retry; an explicit repost may
  retry an unposted result only after complete reads and the normal budget checks.
A review file not produced by a runner is never a review. If neither an
independent reviewer nor a reviewer within the valid owner exception below can
run, delivery waits for the owner.

### Owner-approved contributor-review exception

This alignment is recorded on October 7, 2026, from the owner's
[#264 ruling](https://github.com/feder-positronics/loop-zero/issues/264#issuecomment-6025584110)
and [scope confirmation](https://github.com/feder-positronics/loop-zero/issues/264#issuecomment-6031084292).
For the mixed-authoring route confirmed there (following Marcin's IntelFlo
#6234 direction and delivered in IntelFlo #6327), the owner permits the
less-writing family to perform genuine native review rather than block delivery
solely on family independence.
This is a bounded owner-approved contributor-review exception with reduced
assurance, not proof of an independent-family review.

Under this route, the squash commit truthfully names the main contributing
model in its `Co-Authored-By` trailer. Disclose every actual minority writing
model and its work in the commit body and a PR comment; the PR narrative and
comment must label the review as an **owner-approved contributor-review
exception**. Minority disclosure in prose instead of a trailer is specifically
authorized for this owner route. It is not permission for anyone to manipulate
trailers, invent a main author, silently discard attribution, or use an automatic
waiver. Identify the main contributor and less-writing family from the actual
work, without calculated percentages or a numerical minority threshold.

The native selector excludes recognized trailer families throughout the PR
lineage, from its configured-base merge-base through HEAD. Prose disclosure and
a main-author-only HEAD cannot remove earlier minority-family trailers. Before
the first primary review, the authorized route may squash and rewrite the branch,
preserving truthful main-contributor attribution on the resulting commit and
full minority contribution disclosure in its body and a PR comment, then push
that head for review. GitHub's merge-time squash cannot establish this route.
After a primary review, using the remaining delta requires retaining the exact
reviewed primary commit as an ancestor. Only unreviewed repair commits may be
rewritten for this purpose, and only where the authorized disclosure route and
truthful attribution permit it; do not credit the main contributor with a repair
commit they did not write. If selecting the exception would require rewriting
the primary or its ancestors, the exception does not authorize that rewrite to
obtain a fresh review budget: delivery waits for owner direction. After delta
exhaustion, the existing class-wide audit and fresh-lineage rule still applies;
neither reviewed commit may remain an ancestor before the next primary. This
exception changes neither selector behavior nor the review budget.

Prose disclosure neither changes selector behavior nor proves permission. The
existing mixed-trailer diagnostic remains correct whenever the submitted lineage
contains both recognized families, including an attempted owner exception: no
native independent route exists for that lineage, and owner permission does not
change the selector's result. This exception adds no parser, flag, persistence,
reviewer model or relaxed gate. Genuine runner review on the exact head, checks,
findings, readiness and merge requirements still apply. Gemini remains parked
under closed issue #264; it is not a new reviewer route. Cases outside this
confirmed owner scope keep the default all-writer trailers and closed route.

## Sandbox rules for checks

Every command in `[checks].commands` runs with:

- The source tree read-only, writes to scratch, no write access to `.git`.
- An isolated `HOME`; only `[checks].ro_paths` from your home are visible.
- No network unless `[checks].network = true`.
- Environment reduced to the allowlist (`PATH HOME LANG LC_ALL TERM` default).

If `bwrap` cannot run, `loopzero check` fails with `SandboxUnavailable`; it
never runs checks unsandboxed. Checks and reviews recheck HEAD and dirtiness
after running; changes during the run invalidate the result.

Reviews also require `bwrap` and never fall back to the host. They receive a
read-only worktree and Git directories, a private HOME, reviewer runtime paths,
and network access. Live credentials are bound read-write so OAuth refresh
persists: Claude's config directory (`~/.claude` or `CLAUDE_CONFIG_DIR`) and
`~/.claude.json`; Codex's `auth.json` only, under `CODEX_HOME` or `~/.codex`.
The sandbox bounds readable data; it cannot prevent exfiltration over the
required network.

At native review launch, checks, publishers and `delivery.reviewer_ro_paths`
are read from one verified remote PR-base revision. A missing remote ref or base
workflow refuses native launch; bootstrap workflows need explicit `--config`.
That operator choice is printed and still receives protected-state validation.
Reposts and `check`, `ready`, `merge` and `status` retain their fallback behavior.
Normal mounts may not overlap private reviewer state or standard/custom native
auth/state roots, including source/Git and system mounts. Existing live auth
binds remain intentional exceptions; they do not establish history isolation.
An implicit runtime inside a state directory mounts only the selected executable
file read-only at its resolved path. An explicit runtime path gets no exception;
bundles needing protected sibling assets require an installation outside state.
This protects known native state paths, not every possible host secret.

## Failure

Every command fails closed and loud: a missing tool or a nonzero exit prints
the command and the tail of its output and stops. GitHub reads retry transient
failures up to three times (5/20/60 seconds), excluding rate limits; Mergify reads
retry server errors, throttling and failed reads up to twice (2/5 seconds). The
optional ai-accounts shim retries `loopzero` once after a usage-limit exit (4);
everything else fails once and loud.

Keep that property when chaining commands: never pipe a `loopzero` command
into `tail`, `grep` or `head` (the filter's exit status replaces the verdict);
redirect output to a file, chain with `&&` under `set -o pipefail`, and treat
exit 3 (waiting) and 5 (changes requested) as outcomes, not crashes. The last
line of output states the verdict. Report a `loopzero` outcome with its exit code
and that last line; never infer queue or merge state from an earlier progress line.

Once the merge lands, `loopzero merge` deletes its worktree; enqueueing without
`--wait` returns 0 without cleanup. Run it from the repository root in a
subshell, `(cd <worktree> && loopzero merge --wait)`: an agent shell left
standing in the deleted directory fails its next command with a `getcwd` error
that looks like a failed merge.

Wait with `loopzero ready --wait` and `loopzero merge --wait`, never with a
hand-written `gh` loop. Any other poll needs a deadline, visible progress,
stderr kept, and a stop on command errors: a silent loop looks like running CI.
Run one waiter per PR. Do not poll with `gh pr view`, `gh pr checks`, or
`gh pr list`; their hidden GraphQL requests share the account's quota across
sessions. For one-off PR reads or comments, use the relevant REST `gh api`
endpoint. A quota error stops the command until a verified reset or
`Retry-After`; REST `/rate_limit` does not establish GraphQL capacity.
