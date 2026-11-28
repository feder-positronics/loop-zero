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
authenticate a candidate and fail closed. If candidate authentication blocks
delivery, follow the contract's withdrawal rules to obtain a fresh candidate
event. Source PRs can be refreshed with the eligibility
workflow's manual PR-number input.

Keep the local required-check list, Mergify `queue_conditions` and the effective
GitHub ruleset on exactly the same context names (`checks` and
`Loop-zero Eligibility` here). The
queue injects the GitHub rules at admission and landing. Candidate CI tests the
real target including all earlier queued changes and never deploys. Never select a
cumulative risk lane using only Mergify's `checking_base_sha`, which can already
include earlier queued work. Mergify may directly merge a current PR using
already-valid integration checks and documents no switch to disable that; do not
promise every PR a temporary branch. Do not reduce parallel checks to one unless
the queue rule sets an explicit batch size above one: one check of one PR can
update its source head in place.

When a parent batch has vanished from live status, attestation still checks every
live source it discovers and requires the candidate's complete tree to equal a
clean integration of those pinned source heads onto current main, in source order
derived from the candidate's first-parent merge topology. A temporary bare Git
store fetches SHA-pinned objects using the GitHub CLI's token; it never checks out
or executes candidate content. Ambiguous topology, conflicts, missing objects,
resource limits or unequal trees fail closed and identify missing batches. Main
and the live attestation inputs are reread before success. Squashed parents can
pass; unavailable live predecessors behind a missing batch and unrelated main
advances may still reject. The fallback requires POSIX file-size limits and Git with
`merge-tree --write-tree`; it is bounded to 32 sources, 100,000 reachable objects,
256 MiB on disk, 8 MiB command output and 120 seconds of Git work. Fetches start at
depth 200 and deepen within those bounds when merge history is incomplete.

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

The Mergify admin application key is needed because queue read endpoints offer no
narrower scope; a CI-scoped key or an Actions installation token cannot read queue
membership. In Actions, keep `MERGIFY_API_KEY` in the `mergify-metadata`
environment, restricted by a custom branch policy to `main`, never as a repository
secret; the workflow's `environment.deployment: false` avoids deployment objects.
When moving a key into that environment, read the environment and branch policy
back, verify that a default-branch publisher run can read queue status and that a
non-main run is rejected, and only then delete the repository-scoped copy. A token
that cannot inventory organization secrets gives no basis to delete them.

## Failed candidate checks

After a failed-check dequeue, `loopzero merge --wait` reports the available
failure details. These can include the Mergify report link, failed checks,
whether Mergify isolated the PR, and links to failed GitHub Actions jobs.
It looks up checks if Mergify only links the candidate and scans
the last 256 Ki characters of up to three job logs, preferring jobs in reusable
workflows, and shows up to ten distinct test IDs per log, including pytest
parameters. Open the job link for the full log if the report has no test IDs.

For a lint failure in **Architecture docs and size budget**, open that step in
the job log. A size budget fails when the count is above its cap and grew
against the logged base. The `headroom` field gives the number of lines left;
a negative value gives the overage. The step can also fail earlier with a
message naming a missing architecture guide update or `Architecture-Review`
trailer. Follow the architecture acknowledgement rules in [Architecture](ARCHITECTURE.md)
for that failure.

Before queueing a PR that adds lines near a cap, fetch current main. In a
disposable checkout of the PR head, merge `origin/main` with
`git merge --no-commit --no-ff origin/main`. After a clean merge, run
`scripts/size_budget.sh origin/main` on that tree. For an existing failure,
use the failed job's checkout SHA and `BASE_SHA` to reproduce the original
comparison. A speculative candidate can include earlier queued changes, so
passing against current main can still leave the candidate over budget.
Reduce the flagged count if the PR grows it beyond the cap against main.
If the overage depends on earlier queued work, check again against main
after that work lands or leaves the queue. Retry when the new integration
fits, or when the flagged count holds steady or shrinks against the new base.

## Recovery

If the queue misbehaves, stop admission and prevent Mergify landing, restore the
captured guardrail rules and remove the Mergify-only update restriction before
resuming the original path (the native queue for loop-zero). Each consumer
documents its own recovery settings. Never admit to native and Mergify queues at once. Public plans
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
