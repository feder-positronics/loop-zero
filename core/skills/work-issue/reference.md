# Work Issue Delivery Safeguards

Read the relevant section when its condition applies.

## Implementation Boundaries

- When an API shape changes, regenerate its checked-in schema or client artifacts
  and validate callers against the generated result.
- When acceptance depends on seeded browser state or a changed prerequisite,
  prove it on a fresh stack rather than only an already-running local stack.
- Before the review, record whether the change affects documentation and either
  update it or give the evidence-backed reason no update is needed.
- Before publication, inspect the issue and linked artifacts for a lifecycle
  blueprint owned by this exact issue. If one exists, include its required
  completion in the closing delivery; a child issue does not own its parent's
  blueprint.

## Evidence Shown For Approval

- Any count, census, or state presented to the requester for a decision carries
  its provenance on the same line: environment, service or host, how the
  connection was resolved, and the time read. Local and staging data are never
  labelled production.
- A request to approve a production change restates that provenance. If the
  source turns out wrong, say so, void the approval, and ask again.

## Collision, Waiting, And Re-entry

- Where the base has no merge queue, a branch that is merely behind does not
  need a rebase while implementation or repairs continue. Rebase once, when the
  change is otherwise finished, then run checks and request the exact-head
  review: a review taken before that final rebase is spent for nothing.
  Serialize final integration across parallel tasks instead of racing rebases.

- Treat the issue as singly owned. Recheck branches and PRs before the first edit,
  before publication, and before merge; if another delivery appears, stop and
  reconcile rather than continuing concurrently.
- Waiting for required review, CI, or an observable post-merge acceptance
  condition remains part of this issue task. Do not bypass the wait or start a
  different issue in the same task.
- After interruption or context loss, re-read the issue, PR head, remote branch,
  CI, and open review threads before resuming. Never rely on remembered state.

## Late Evidence

- Reply to each blocking thread with its fix commit before resolving it.
- A late finding after merge gets a fresh follow-up branch and PR. Reopen the
  issue only when a previously satisfied acceptance criterion has regressed.
- Ordinary monitoring after shipped acceptance is separate work. Later evidence
  gets a new, explicitly requested review or issue; do not create reminder issues.
- Deferred work does not extend this delivery. Take it up only as a separately
  requested task, preferably in a fresh session.
