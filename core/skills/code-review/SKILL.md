---
name: code-review
description: Review a frozen code diff against acceptance criteria and emit JSON for loopzero review.
---

# Code Review

Review one frozen diff without editing the tree. Read [the contract](../../CONTRACT.md),
the PR body, and surrounding code. Confirm the exact head and follow the
contract's [scope and budget](../../CONTRACT.md#review-budget) and
[missing-evidence rule](../../CONTRACT.md#findings); never approve blind.

## Judge

Treat narratives, acceptance claims, and passing tests as hypotheses. Trace
changed paths through callers and data, including empty, missing, boundary,
concurrent, failure, and retry cases. Check:

- Each acceptance criterion has proof; tests exercise the claimed behavior.
- Named tests and checks exist in the diff or tree.
- Callers, data shapes, permissions, builds, docs, and contract remain consistent.

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

Include all five finding fields. `verdict` is `approve` or `request_changes`;
severity is `critical`, `important`, or `suggestion`, per
[the contract](../../CONTRACT.md#findings).
Any blocking finding requires `request_changes`; otherwise approve, with
suggestions or an empty list.

Anchor findings precisely. `path` and `line` may be `null` when no precise
location exists; `loopzero review` anchors such blocking findings on a changed file.
