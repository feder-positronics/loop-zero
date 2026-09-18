---
name: review
description: Independently review a frozen diff against its acceptance criteria and report defects as structured findings with severity.
---

# review

Read [the contract](../../CONTRACT.md). You review the exact head you are
given; you do not edit it. Read the PR body (objective, acceptance, checks),
the full diff, and enough surrounding code to judge the diff. For a delta
review, read only the diff since the reviewed commit plus the open threads.

## Judge

- Does the change meet each acceptance line? Missing proof is a finding.
- Would it break a caller, a data shape, a permission boundary or a build?
- Is any test asserting less than the objective claims?

Report demonstrated failures with a failure condition and impact. Separate
"I could not verify" from "this is wrong". Generic advice is not a finding.

## Severity

- `critical`: wrong, unsafe or data-losing; must be fixed.
- `important`: ships a bug or misses acceptance; must be fixed.
- `suggestion`: better but optional. Never blocks, never counted.

## Output

Return exactly one JSON object and nothing else:

```json
{
  "verdict": "approve" | "request_changes",
  "findings": [
    {
      "severity": "critical" | "important" | "suggestion",
      "path": "src/file.py" | null,
      "line": 42 | null,
      "title": "one line",
      "body": "what fails, when, and the smallest fix"
    }
  ]
}
```

`verdict` is `request_changes` when any finding is `critical` or
`important`, otherwise `approve`. An empty `findings` list with `approve` is
a valid, complete result. If you cannot see the diff, return one `critical`
finding saying so; never approve blind.
