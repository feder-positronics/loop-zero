# Resolve Findings Reference

## Staleness re-verification

When the head moves:

1. Identify primary/delta reviewed SHA and current PR head.
2. Re-open the cited path and surrounding behavior on current head; line
   numbers alone prove nothing.
3. Run the smallest reproduction or inspect current acceptance evidence.
4. Classify: reproducible, fixed by named commit, or unsupported by current code.
5. Hand off reply evidence; the next step posts and resolves with
   `loopzero resolve <id> "<fix commit or evidence-backed reason>"`, making
   the fix or reason visible in the thread.

## Response handoff

For each thread provide URL/stable location, severity, current-head
applicability, failure/acceptance conditions, chosen response, strongest
rejected alternative when material, affected files, validation and exact reply
to post after applying the response.
