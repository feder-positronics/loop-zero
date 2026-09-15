# Review bar for authority code

Applies to any change in review admission, slot reservation and settlement,
content generations, trust claims, credential sealing, sandbox mounts, the
authority ledger and its compaction. Written after the D29 work, where each
of the rules below cost at least one review round before it was stated.

## Method

1. **Probes first.** Every finding from a review is committed as a test in the
   consumer's real record shapes (package-built receipts, real envelopes),
   confirmed failing on the head under review, before the code changes. A test
   that passes on the parent commit proves nothing and is rejected.
2. **Fail closed on rare paths.** Released retries, successors after consumed
   deltas, manifest changes after a release: when the exact rule is contested,
   the path returns a full primary review rather than a carry. One extra review
   a month is cheaper than one unearned verdict.
3. **Two reviewers, in parallel, from a different model family than the
   builder.** Reviewers run in a disposable clone with an integrity check that
   voids the verdict if the tree changes; they run their probes rather than
   reason from the diff.
4. **Soundness blocks, liveness follows up.** A review classifies each finding.
   Soundness: an unearned verdict, a slot or budget bypass, a caller label
   overriding package derivation, a leak, authority lost across compaction.
   Liveness: a conservative extra review, a blocked retry that could be
   admitted, an extra owner decision. Only soundness blocks merge; liveness
   becomes an issue.
5. **Reviewer probes are the builder's pre-delivery gate.** The builder runs
   the previous round's probes before delivering, so a review sees a head that
   already survived its own last attack.

## Rules the code must satisfy

1. Proofs and receipts are authenticated by provenance, never by shape or
   self-digest; a fabricated record with a correct digest is rejected by a test.
2. Nothing the caller supplies can label, widen or bypass: intents, families,
   reasons, changed paths, security paths and authentication flags are derived
   by the package and compared against what the caller claims.
3. Identity is content, not commit: amend, re-push, cherry-pick, revert and
   rename resolve to the same generation; a different base is a real change
   and inherits nothing.
4. A slot is freed only by verified evidence (an authenticated releasing
   outcome or verified process death), never by a timer, a caller assertion or
   a replayed settlement; unresolved stays held; an idempotent reservation
   response is not a second launch permission.
5. Verdict-bearing and non-verdict intents are distinct; adjudication and
   discovery never reserve, consume or carry a verdict, and their terminals are
   never a coverage source through any predicate.
6. Whatever takes an authenticated view returns a view; a plain list downstream
   of a view is a bug.
7. Unknown is never zero; a numeric cost with unknown provenance is rejected at
   construction.
8. Coverage is by path component prefix, never string equality; claims without
   coverage depend on everything; released obligations carry forward and are
   discharged only by an authenticated verdict per claim.
9. The lock is held for reload, validate and append, never during inference;
   settlement asserts it like reservation does.
10. Legacy records project conservatively: one primary per historical identity,
    family from the terminal's authenticated intent, no resurrection of slots,
    no silent drops.
11. Every error is content-free but carries its class name; no credential, path
    or identity in messages.
12. Tests prove the negative for each rule above and fail on the parent commit.
