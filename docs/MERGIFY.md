# Mergify operations

loop-zero delivers through explicit admission to the serial Mergify `main` queue:
one source PR per batch, up to three speculative checks and squash merges of the
original PRs. The queue owns integration; keep authored heads unchanged when only
main advances. Product tests and model review remain separate evidence.
[Setup](../SETUP.md#mergify) owns admission, credentials, source-head preservation
and configuration verification; [hosted eligibility](../SETUP.md#hosted-review-eligibility)
owns review publishers, secret isolation, status identity and the
withdraw/edit/refresh/readmit protocol.

## Publisher and integration constraints

Mergify carries queue check conditions into its temporary candidates, so a
source-only eligibility publisher is insufficient. The candidate attestor verifies
live vendor membership, candidate identity, all constituent source heads and their
review eligibility before publishing success. Candidate success is published only
from an `opened` or `synchronize` `pull_request_target` event whose sender is the
verified Mergify bot (App `mergify` 10562, `mergify[bot]` 37929162; recheck the live
installation, never trust a display name). Manual and workflow-run refreshes cannot
authenticate a candidate and fail closed: dequeue and request again so Mergify
supplies a fresh event. Source PRs can be refreshed with the eligibility
workflow's manual PR-number input.

Keep the local required-check list, Mergify `queue_conditions` and the effective
GitHub ruleset on exactly the same context names (`checks` and
`Loop-zero Eligibility` here; `PR Policy` and `PR Quality Gate` in IntelFlo). The
queue injects the GitHub rules at admission and landing. Candidate CI tests the
real target including all earlier queued changes and never deploys. Never select a
cumulative risk lane using only Mergify's `checking_base_sha`, which can already
include earlier queued work. Mergify may directly merge a current PR using
already-valid integration checks and documents no switch to disable that; do not
promise every PR a temporary branch. Do not reduce parallel checks to one: that can
enable source-head updates.

When a parent batch has vanished from live status, attestation still checks every
live source it discovers and requires the candidate's complete tree to equal a
clean integration of those pinned source heads onto current main, in source order
derived from the candidate's first-parent merge topology. A temporary bare Git
store fetches SHA-pinned objects using the GitHub CLI's token; it never checks out
or executes candidate content. Ambiguous topology, conflicts, missing objects,
resource limits or unequal trees fail closed and identify missing batches. Main
and the live attestation inputs are reread before success. Squashed parents can
pass; unavailable live predecessors behind a missing batch and unrelated main
advances may still reject. The fallback requires Linux `prlimit` and Git with
`merge-tree --write-tree`; it is bounded to 32 sources, 100,000 reachable objects,
256 MiB on disk, 8 MiB command output, history depth 200 and 120 seconds of Git work.

## Protection and credentials

The JSON files under `.github/rulesets/` are reviewed configuration artifacts; no
workflow applies them.

- `main.json` requires PRs, `checks` and `Loop-zero Eligibility`, protects
  deletion and non-fast-forward updates, and has **no bypass actors**, including
  Mergify.
- `mergify-owner.json` separately restricts main updates to Mergify App 10562.
  Its bypass grants no exemption from the guardrail ruleset, so ordinary writers
  cannot merge around the queue or push to main. The installed App's permission
  grant and ruleset bypasses are separate; never replace this with a blanket
  bypass on the guardrail set, and prepare any broader authority as a concrete
  change for review.
- `main-before-mergify.json` is the pre-cutover rollback payload. Capture a fresh
  live snapshot before applying anything; never overwrite unrelated live changes.

The Mergify admin application key is needed because queue read endpoints offer no
narrower scope; a CI-scoped key or an Actions installation token cannot read queue
membership. In Actions, keep `MERGIFY_API_KEY` in the `mergify-metadata`
environment, restricted by a custom branch policy to `main`, never as a repository
secret; the workflow's `environment.deployment: false` avoids deployment objects.
When moving a key into that environment, read the environment and branch policy
back, verify that a default-branch publisher run can read queue status and that a
non-main run is rejected, and only then delete the repository-scoped copy. A token
that cannot inventory organization secrets gives no basis to delete them.

## Recovery

If the queue misbehaves, stop admission and prevent Mergify landing, restore the
captured guardrail rules and remove the Mergify-only update restriction before
resuming the original path (loop-zero: native queue; IntelFlo: strict up-to-date
squash delivery). Never admit to native and Mergify queues at once. Public plans
may lack a pause API, so rollback revokes queue authority through reviewed
changes. Keep all required checks; an outage does not justify lowering protection.
After any queue configuration change, drain first and verify vendor draft-PR
placement before resuming; GitHub blob identity does not prove Mergify ingested it.

Hosted eligibility reads trusted publishers from `main`, so a PR restoring a
removed publisher cannot approve itself. Break-glass is owner-only: add a
temporary PR-only bypass to the guardrail ruleset, merge the restoring PR, remove
the bypass immediately, then refresh eligibility on every open PR and requeue
candidates.

## Sources

- https://docs.mergify.com/merge-queue/rules/
- https://docs.mergify.com/merge-queue/batches/
- https://docs.mergify.com/merge-queue/github-rulesets/
- https://docs.mergify.com/commands/queue/
- https://docs.mergify.com/api/usage/
- https://docs.mergify.com/api/merge-queue/
