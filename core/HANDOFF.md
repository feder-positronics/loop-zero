# Handoff template

`loopzero start` writes this to `.loopzero/task.md`. You fill Objective, Acceptance
and Notes; the tool fills Base, Checks and Review and uses the file as the PR body.

```markdown
## Objective
One paragraph: what changes and why it is worth a PR.

## Acceptance
- [ ] Observable condition 1 (a test, a command, a screenshot)
- [ ] Observable condition 2

## Base
<base branch> @ <base SHA>   (written by the tool)

## Checks
(filled by `loopzero pr`: command, exit code, duration per check)

## Review
(filled by `loopzero review`: family, reviewed head, verdict, blocking count)

## Notes
Anything the reviewer must know: trade-offs, follow-ups you chose not to do.
```
