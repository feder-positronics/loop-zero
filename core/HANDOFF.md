# Task handoff

Record these in the existing task/PR; reuse existing fields and evidence links:

- Objective and acceptance criteria.
- Repository, branch, base and current commit; dirty changes if present.
- Core source revision and selected profiles from `workflow.toml`.
- Current owner/session, allowed paths and any shared-resource reservation.
- Completed changes and exact commands, working directories and results.
- Independently reviewed revision, review evidence and unresolved findings.
- Blockers, remaining merge conditions and the next action with its owner.

Transfer a frozen review candidate explicitly between runtimes. The reviewer
gets the exact patch and acceptance evidence; the previous writer remains
stopped until ownership returns. Do not label a held candidate completed or
merged. Preserve earlier READY evidence when a later repair changes the head.
