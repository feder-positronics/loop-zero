# Mergify adoption

The proposed workflow for both repositories uses explicit queue admission, serial
cumulative integration, one source PR per batch, three speculative checks and
squash merges of original PRs. After cutover, the queue owns integration; agents
keep their authored heads unchanged when only main advances. Product tests and
model review remain separate evidence.

## Dependencies

1. Install the Mergify GitHub App for loop-zero and IntelFlo. The verified public
   App is `mergify` (10562), with author `mergify[bot]` (37929162). Recheck the live
   installation rather than granting an actor by display name.
2. Create a Mergify admin application key; the vendor's queue read endpoints do
   not offer a narrower application-key scope. Store local CLI credentials in
   `MERGIFY_API_KEY` or private `~/.config/mergify/api-key`. In Actions, create a `mergify-metadata`
   environment restricted to deployments from the protected default branch and
   store its `MERGIFY_API_KEY` there, never as a repository secret. A CI-scoped key
   and a GitHub Actions installation token cannot read queue membership.
3. Deliver shared authored-head eligibility and Mergify CLI support before
   enabling this configuration. Set trusted review publishers to the GitHub
   accounts that actually post model reviews (currently `eowca`).
4. Prove source and candidate publishers and existing CI on a disposable target.
   A source-only eligibility publisher is insufficient: Mergify carries queue
   check conditions into its temporary candidate checks. The candidate publisher
   must verify live vendor membership, candidate identity, all constituent source
   heads and their review eligibility before publishing success on the candidate.
   Candidate success is published only from an `opened` or `synchronize`
   `pull_request_target` event whose sender is the verified Mergify bot; manual and
   workflow-run refreshes cannot authenticate a candidate head and fail closed.

Review the environment boundary as configuration before applying it. The exact
GitHub API payloads are:

```json
PUT /repos/feder-positronics/loop-zero/environments/mergify-metadata
{
  "wait_timer": 0,
  "prevent_self_review": false,
  "reviewers": [],
  "deployment_branch_policy": {
    "protected_branches": false,
    "custom_branch_policies": true
  }
}
```

```json
POST /repos/feder-positronics/loop-zero/environments/mergify-metadata/deployment-branch-policies
{"name": "main", "type": "branch"}
```

After creating that policy, move `MERGIFY_API_KEY` into the environment, verify a
default-branch publisher run can read queue status, and only then delete the
repository-scoped secret. Read the environment and branch-policy APIs back before
cutover. The workflow sets `environment.deployment: false`, so using the environment
for its secret and branch policy does not create deployment objects. These are
reviewed payloads; this repository does not apply them.

## Admission and CI

`loopzero merge --wait` requests `@mergifyio queue main`, confirms membership via
Mergify's API, and waits for GitHub to report the original PR MERGED. A submitted
comment or `Mergify Merge Queue` badge does not prove membership. New source
commits need new review evidence and explicit delivery intent. No persistent
label automatically requeues a pushed branch.

After the trusted publisher first reaches `main`, refresh existing source PRs with
the eligibility workflow's manual PR-number input before requesting queue admission.
A temporary candidate cannot be refreshed manually: dequeue and request it again so
Mergify creates or updates the candidate and supplies a fresh authenticated event.

Keep the `checks` required check in loop-zero, and `PR Policy` plus
`PR Quality Gate` in IntelFlo. The queue injects the GitHub rules at both admission
and landing. Test temporary draft PRs against the real target branch, including
all earlier queued changes. Never select the cumulative risk lane using only
Mergify's `checking_base_sha`, which can already include earlier queued work.

Mergify may directly merge a current PR using already-valid integration checks.
It documents no disable switch. With no scopes configured, prove that this path
requires green exact-head eligibility and existing CI, and that behind/cumulative
PRs get fresh integration candidates. Do not promise every PR gets a temporary
branch. Do not reduce parallel checks to one: that can enable source-head updates.

## Reviewed protection proposal

These JSON files are review artifacts; no workflow automatically applies them.

- `.github/rulesets/main.json` replaces ruleset 23666528. Its only behavioral
  changes are removing GitHub's native merge queue and requiring
  `Loop-zero Eligibility`; existing PR, `checks`, deletion and non-fast-forward
  rules remain. Strict status checking was already false.
  This ruleset has **no bypass actors**, including Mergify.
- `.github/rulesets/mergify-owner.json` is a separate update restriction for main.
  Only Mergify App 10562 bypasses that restriction. Its bypass grants no exemption
  from the independent guardrail ruleset. Ordinary writers cannot merge around
  the queue or push directly to main.
- `.github/rulesets/main-before-mergify.json` is the inspected rollback payload.
  Capture another live snapshot immediately before applying: other sessions can
  change configuration. Do not overwrite unrelated live changes with this file.

The installed App's permission grant and these ruleset bypasses are separate.
Do not replace this narrow proposal with a blanket bypass on the guardrail set.
Verify that Mergify works with those remaining rules on the disposable target;
if it needs broader authority, prepare that concrete change for review first.
IntelFlo's corresponding strict-to-queue transition requires the approval reserved
by its review-gate policy after the diff and scenario evidence are prepared.

The supplied publisher uses a GitHub Actions commit status. GitHub and Mergify
match that status by context name, so a repository writer who can add a workflow
can forge it with `GITHUB_TOKEN`. The protected environment keeps the Mergify key
out of branch workflows but cannot solve status identity. Before cutover, record
explicit owner acceptance of this cooperative writer boundary, or replace the
publisher with a dedicated GitHub App and bind the ruleset check to its integration
ID. Branch names, PR bodies and untrusted event metadata alone remain insufficient
to pass the candidate attestor.

## Cutover proof and rollback

Freeze landing briefly and drain the native queue before replacing its authority.
Never admit to native GitHub and Mergify queues simultaneously on main. Read back
effective rules before admitting reviewed work.

Prove: two reviewed siblings land without source rewrites; a pushed head loses
eligibility; dismissed reviews and supported finding mutations withdraw eligibility;
individually passing but jointly failing changes do not land; candidate removal
rebuilds dependents; a forged queue prefix is rejected; CLI restart resumes without
another request; API errors never fall back to direct merge; only Mergify can update
main; temporary candidate CI never deploys. IntelFlo additionally proves combined
migration-head and migration-prerequisite behavior.

If verification fails, stop admission and prevent Mergify landing, then restore
the captured guardrail rules and remove the Mergify-only update restriction before
resuming the original path. loop-zero returns to its native queue; IntelFlo returns
to strict up-to-date squash delivery. Public plans may not offer a pause API, so
rollback must include reviewed revocation of queue authority. Keep all required
checks. Do not silently lower protections during an outage.

Finding mutations use the supported withdraw/edit/refresh/readmit protocol in
SETUP.md. Actions does not cover every thread-reopening event and an asynchronous
publisher cannot make metadata changes atomic with a merge. This limitation must
be accepted explicitly before unattended landing; no custom scheduler or ledger
is introduced to hide it.

## Sources

- https://docs.mergify.com/merge-queue/rules/
- https://docs.mergify.com/merge-queue/batches/
- https://docs.mergify.com/merge-queue/github-rulesets/
- https://docs.mergify.com/commands/queue/
- https://docs.mergify.com/api/usage/
- https://docs.mergify.com/api/merge-queue/
