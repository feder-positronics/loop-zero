---
name: code-review
description: Review a frozen code diff against its acceptance criteria and report defects as structured findings with severity; use for code diffs, not broader assessments.
---

# Code Review

Review one frozen diff against its acceptance criteria and return structured
findings. Do not edit the reviewed tree.

Read [the contract](../../CONTRACT.md), the PR body, and enough surrounding
code to judge the change. Confirm the exact head. Review the full diff for a
primary and only the diff since the reviewed commit for a delta. Follow the
contract's default independence rule or its bounded owner-approved
contributor-review exception; the latter has reduced assurance and must be
labeled in the PR narrative and comment.

## Judge

Review with professional skepticism. Treat the PR narrative, acceptance
claims, and passing tests as hypotheses: verify that each test exercises the
claim it is cited for. Trace each
changed path through its callers and data; check edge cases (empty, missing,
boundary, concurrent, failure, retry); and look for inconsistencies between
code, tests, docs, and the contract.

- Does the change meet each acceptance line? Missing proof is a finding.
- Would it break a caller, data shape, permission boundary, or build?
- Is any test asserting less than the objective claims?
- Does every test or acceptance check named in the PR body or docs exist in the diff or tree?

When a failure is one instance of a class (a missed caller, reader, state
transition, or retry path), find and report the other instances in the same
review; a sibling discovered next round is a miss of this one.

Report demonstrated failures precisely and constructively: anchor, failure
condition, impact, and the smallest fix. Separate "could not verify" from
"is wrong". Generic advice is not a finding.

Use the [contract's severities](../../CONTRACT.md#findings): `critical` and
`important` block; `suggestion` never does.

## Output And Exit

Return exactly one JSON object and nothing else. `verdict` is `approve` or
`request_changes`; every finding has severity `critical`, `important`, or
`suggestion` and includes all five fields shown here:

```json
{
  "verdict": "approve",
  "findings": [{
    "severity": "suggestion",
    "path": "src/file.py",
    "line": 42,
    "title": "Concise title",
    "body": "Evidence, impact, and the smallest improvement"
  }]
}
```

`path` and `line` may be `null` when no precise location exists; `loopzero
review` anchors such blocking findings on a changed file. Request changes when any blocking finding
exists. Approve when none exists; approval may include suggestions or an empty
list. If the diff or required evidence is unavailable, return one `critical`
finding saying so; never approve blind.
