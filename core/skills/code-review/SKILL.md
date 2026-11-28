---
name: code-review
description: Advisory review of a frozen code diff against acceptance criteria, returning JSON findings; `loopzero review` does not load it.
---

# Code Review

Review one frozen diff without editing the tree. Read [the contract](../../CONTRACT.md),
PR body and surrounding code. Confirm the exact head. Follow
[review budget](../../CONTRACT.md#review-budget) and [findings rules](../../CONTRACT.md#findings).
Apply [local check receipt reuse](../../CONTRACT.md#check-reuse-and-delivery-priority).

## Judge

Treat narratives, acceptance claims, and passing tests as hypotheses. Trace
changed paths through callers and data, including empty, missing, boundary,
concurrent, failure, and retry cases. Check:

- Each acceptance criterion has proof; tests exercise the claimed behavior.
- Named tests and checks exist in the diff or tree.
- Callers, data shapes, permissions, builds, docs, and contract remain consistent.
- Maintainability: flag workarounds that leave the real defect in place, duplicated
  decision logic, unnecessary shims/layers, or added code where simplifying or
  deleting would do (`important` when it creates ongoing defect or divergence risk;
  `suggestion` otherwise).

Audit sibling callers, readers, transitions, and retries when a defect reveals
a class; report its instances together. Distinguish unavailable proof from
demonstrated defects. Findings need an anchor, failure condition, impact, and
smallest fix; generic advice is not a finding.

## Output And Exit

Return exactly one JSON object, with no surrounding text:

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

Include all five finding fields; `path` and `line` may be `null` when no precise
location exists. `verdict` is `approve` or `request_changes`;
severity is `critical`, `important`, or `suggestion`, per
[the contract](../../CONTRACT.md#findings).
Any blocking finding requires `request_changes`; otherwise approve, with
suggestions or an empty list.
