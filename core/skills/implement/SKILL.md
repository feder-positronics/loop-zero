---
name: implement
description: Deliver an understood bounded change with its owning behavior proof and repository-required checks.
---

Read [the shared contract](../../CONTRACT.md), local acceptance and relevant
selected profiles. Make the smallest complete change under the repository's
testing contract. Run the owning checks and record actual commands/results;
broaden validation when the changed behavior or local policy warrants it.

Stop on scope or ownership conflict. A failed check requires diagnosis or an
explicit blocker; do not weaken acceptance to obtain a pass. Submit the exact
candidate for the independent review required for a real pilot change. Resolve
important findings and verify repairs according to local review policy.
Finish with the [handoff](../../HANDOFF.md), distinguishing READY from merged.
