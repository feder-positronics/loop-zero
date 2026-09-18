# Resolve Findings Reference

## Staleness Re-Verification

Before trusting a finding after the PR head moves:

1. Identify the head SHA reviewed by the primary or delta review and the current
   PR head.
2. Re-open the cited path and surrounding behavior on the current head; line
   numbers alone are not evidence.
3. Re-run the smallest relevant reproduction or inspect the current acceptance
   evidence.
4. Classify the thread as still reproducible, already fixed by a named commit, or
   no longer supported by the current code.
5. Reply in the thread with that evidence. Resolve it only after the fix or
   reason is visible there.

For a delta review, inspect only the diff since the reviewed commit and the open
threads inherited from the primary review. Do not reopen unrelated parts of the
full diff unless new evidence shows the reviewed premise was false.

## Response Handoff

For each thread, hand off:

- PR thread URL or stable location;
- severity and current-head applicability;
- failure and acceptance conditions;
- smallest chosen response and strongest rejected alternative when material;
- files likely to change and validation to run;
- exact factual reply to post after the response is applied.
