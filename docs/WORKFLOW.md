# Workflow at a glance

Plain-language pictures of how a change moves through loop-zero. They summarize
the rules; when a picture and the rules disagree, the rules win. Commands are
shown in small text beside each step.

## 1. From idea to merged

```mermaid
flowchart TD
    A["Start a task<br/><sub>loopzero start</sub>"] --> B["Make the change and commit it"]
    B --> C["Run checks in a sandbox<br/><sub>loopzero check</sub>"]
    C -->|checks fail| D["Fix the problem"]
    D --> C
    C -->|checks pass| E["Open a draft pull request<br/><sub>loopzero pr</sub>"]
    E --> F["Get a review of the exact version<br/><sub>loopzero review</sub>"]
    F -->|changes requested| G["Fix the findings and push"]
    G --> C
    F -->|approved| H["Check it is ready: no open findings,<br/>required CI green on the same version<br/><sub>loopzero ready --wait</sub>"]
    H -->|version changed since review| F
    H -->|ready| I["Merge<br/><sub>loopzero merge --wait</sub>"]
    I --> J["Mergify queue lands it<br/>where one is configured"]
    J --> K["Clean up: delete branch and worktree"]
```

The review and the CI results belong to one exact version (head) of the
branch. If the branch changes after the review, that review no longer counts
and the change is reviewed again. Work stops at "ready" unless merging has been
authorized.

Summarizes [The six steps](../core/CONTRACT.md#the-six-steps) and
[Blockers](../core/CONTRACT.md#blockers); the queue is described in
[MERGIFY.md](MERGIFY.md).

## 2. How many reviews a change gets

```mermaid
flowchart TD
    A["First full review<br/><sub>primary</sub>"] -->|approved| Z["Done: continue to ready"]
    A -->|fixes needed| B["Make all fixes in one batch"]
    B --> C["One review of only the new changes<br/><sub>delta, at most one</sub>"]
    C -->|approved| Z
    C -->|more fixes still needed| D["Audit the whole class of defect,<br/>not just the reported cases"]
    D --> E["Squash so no reviewed commit remains"]
    E --> F["One fresh full review<br/><sub>new primary</sub>"]
    F --> Z
    G["Reviewer gives no verdict:<br/>tool or login failure"] --> H["Costs nothing: try the next<br/>eligible model family"]
    H -->|none can run| I["Wait for the owner"]
```

Each line of work gets one full review and at most one follow-up that reads
only what changed. The reviewer comes from a model family that did not write
any part of the change. A failed reviewer run that
produced no verdict does not use up a review; if no eligible reviewer can run
at all, wait for the owner. Repeated findings of the same kind mean the audit
missed something, not that patching should continue.

Summarizes [Review budget](../core/CONTRACT.md#review-budget).

## 3. Who does what

```mermaid
flowchart TD
    O["Owner<br/><sub>the human; talks only to the decisions owner</sub>"] --> D["Decisions owner<br/><sub>resolves owner decisions within delegated authority</sub>"]
    D --> C["Coordinator<br/><sub>owns delivery of one repository, dispatches workers and reviews</sub>"]
    C --> W["Worker<br/><sub>one bounded delivery from a brief</sub>"]
    W --> P["Pull request with checks"]
    P --> R["Reviewer<br/><sub>independent, another model family by default</sub>"]
    R -->|findings or approval| W
    W -->|blocker or result| C
    C -->|reports| D
```

Each role has one current owner, and state lives in Git and GitHub rather than
in anyone's memory. A worker that is stuck sends one message with its blocker
to its parent and stops instead of asking a person directly.

Summarizes [Team roles](DELEGATION.md#team-roles) and
[Review budget](../core/CONTRACT.md#review-budget).
