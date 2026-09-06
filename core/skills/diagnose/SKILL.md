---
name: diagnose
description: Establish the cause of broken behavior when it remains unclear, using reproduction and evidence before choosing a repair.
---

Read [the shared contract](../../CONTRACT.md). Reproduce the failure with the
smallest useful case, distinguish environment failures from behavior failures,
and test competing explanations when the evidence supports more than one.
Deliver the demonstrated cause, reproduction evidence and smallest repair route.

Do not treat correlation or a plausible explanation as proof. When prerequisites
prevent reproduction, report what is missing and what remains uncertain. Stop
before speculative repairs or changes beyond authorized scope; route an understood
repair to implementation. Diagnosis is not a compulsory delivery phase.
