# Workflow at a glance

Plain-language pictures of how a change moves through loop-zero. They summarize
the rules; when a picture and the rules disagree, the rules win. Commands are
shown in small text beside each step.

## 1. From idea to merged

```mermaid
flowchart TD
    A["Start a task<br/><sub>loopzero start my-task</sub>"] --> B["Make the change and commit it"]
    B --> C["Run checks in a sandbox<br/><sub>loopzero check</sub>"]
    C -->|checks fail| D["Fix the problem"]
    D --> C
    C -->|checks pass| E["Open a draft pull request<br/><sub>loopzero pr</sub>"]
    E --> F["Get a review of the exact version<br/><sub>loopzero review</sub>"]
    F -->|changes requested| G["Fix the findings and push"]
    G --> C
    F -->|approved| H["Check it is ready: no open findings,<br/>required CI green on the same version<br/><sub>loopzero ready --wait</sub>"]
    H -->|review coverage lost and budget allows another review| F
    H -->|ready| I["Merge<br/><sub>loopzero merge --wait</sub>"]
    I --> J["Mergify queue lands it<br/>where one is configured"]
    J --> K["Clean up: delete branch and worktree"]
```

Summarizes [The six steps](../core/CONTRACT.md#the-six-steps),
[Blockers](../core/CONTRACT.md#blockers) (review coverage, head movement and
readiness) and the [review budget](../core/CONTRACT.md#review-budget); the queue
is described in [MERGIFY.md](MERGIFY.md).

## 2. How many reviews a change gets

```mermaid
flowchart TD
    A["First full review<br/><sub>primary</sub>"] -->|approved| Z["Done: continue to ready"]
    A -->|fixes needed| B["Make all fixes in one batch"]
    B --> C["One review of only the new changes<br/><sub>delta, at most one</sub>"]
    C -->|approved| Z
    C -->|findings remain| D["Owner fixes or rejects findings with reasons"]
    D -->|ready| Z
    C -->|split needed| E["Owner splits the PR"]
    G["Reviewer gives no verdict:<br/>tool or login failure"] --> H["Costs nothing: try the next<br/>eligible model family"]
    H -->|none can run| J["Same-family review in a fresh session,<br/>disclosed on the PR"]
    J -->|none can run| I["Wait for the owner"]
```

See the contract's [review budget](../core/CONTRACT.md#review-budget).

## 3. Who does what

```mermaid
flowchart TD
    O["Owner<br/><sub>the human; gives orders to the decisions owner</sub>"] --> D["Decisions owner<br/><sub>resolves owner decisions within delegated authority</sub>"]
    D --> C["Coordinator<br/><sub>owns delivery of one repository, dispatches workers and reviews</sub>"]
    C --> W["Worker<br/><sub>one bounded delivery from a brief</sub>"]
    W --> P["Pull request with checks"]
    P --> R["Reviewer<br/><sub>independent, another model family by default</sub>"]
    R -->|findings or approval| W
    W -->|blocker or result| C
    C -->|reports| D
```

Each role has one current owner, and state lives in Git and GitHub rather than
in anyone's memory.

Summarizes [Team roles](DELEGATION.md#team-roles) and
[Review budget](../core/CONTRACT.md#review-budget).
