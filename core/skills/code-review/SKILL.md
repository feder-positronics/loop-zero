---
name: code-review
description: Independently review a frozen diff against its acceptance criteria and report defects as structured findings with severity; use for code diffs, not broader assessments.
---

# Code Review

Review one frozen diff against its acceptance criteria and return structured
findings. Do not edit the reviewed tree.

Read [the contract](../../CONTRACT.md), the PR body, the full diff, and enough
surrounding code to judge the change. Confirm the exact head before reviewing.
For a delta review, read only the diff since the reviewed commit plus its open
threads.

## Judge

- Does the change meet each acceptance line? Missing proof is a finding.
- Would it break a caller, data shape, permission boundary, or build?
- Is any test asserting less than the objective claims?

Report demonstrated failures with a failure condition and impact. Separate
"could not verify" from "is wrong". Generic advice is not a finding.

## Severity

- `critical`: wrong, unsafe, or data-losing; must be fixed.
- `important`: ships a bug or misses acceptance; must be fixed.
- `suggestion`: optional improvement; never blocks and is never counted.

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

`path` and `line` may be `null`, but every blocking finding needs an anchor;
otherwise use the first changed file. Request changes when any blocking finding
exists. Approve when none exists; approval may include suggestions or an empty
list. If the diff or required evidence is unavailable, return one `critical`
finding saying so; never approve blind.
