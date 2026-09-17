# Design records

Why loop-zero is shaped as it is, and proposed changes under exploration.
Check each document’s stated status: a draft does not supersede an accepted
decision or the active core contract.

| Document | What it answers |
| --- | --- |
| [Workflow migration implementation](2026-09-17-workflow-migration-implementation.md) | Selected #105 implementation proposal: v2 interfaces, authenticated dispositions, executable units and deletion acceptance |
| [Workflow migration and simplification](2026-09-17-workflow-migration-simplification.md) | Refined #105 exploration: alternatives, removal boundaries and cross-project acceptance; linked implementation is owner-authorized |
| [Executable core architecture](2026-09-11-executable-core-architecture.md) | What the package contains, where each mechanism lives, and the boundaries it must not cross |
| [Decision record](2026-09-11-decision-record.md) | Every settled decision, the assumptions in force, and what is still deferred |
| [Independent architecture review](2026-09-11-independent-architecture-review.md) | A second opinion on the architecture from a different model family, with the responses |
| [Review bar for authority code](authority-review-bar.md) | The method and the rules any change to admission, slots, generations, claims, credentials or the ledger must satisfy before merge |
| [Review verdicts as content facts](2026-09-14-review-verdicts-as-content-facts.md) | Why a review verdict is keyed by content rather than by pull request, and how claim-level trust verification works |
| [Preventing delivery blockages](2026-09-16-preventing-delivery-blockages.md) | Proposed prevention strategy for mutable test inputs, renewal loss, interrupted delivery, and operational waits |

Decisions that belong to a particular consumer's repository, such as its
branch protection, environment prefix, throughput thresholds or dependency
wiring, are recorded by that consumer rather than here.
