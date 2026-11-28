# Workflow at a glance

Plain-language pictures of how a change moves through loop-zero. They summarize
the rules; when a picture and the rules disagree, the rules win. Commands are
shown in small text beside each step.

## 1. From idea to merged

```mermaid
flowchart TD
    A["Start a task<br/><sub>loopzero start my-task</sub>"] --> B["Make the change and commit it"]
    B --> E["Open a draft pull request<br/><sub>loopzero pr</sub>"]
    E --> C["Run checks in a sandbox<br/><sub>loopzero check</sub>"]
    C -->|checks fail| D["Fix, commit and push<br/><sub>loopzero pr</sub>"]
    D --> C
    C -->|checks pass| F["Get a review of the exact version<br/><sub>loopzero review</sub>"]
    F -->|changes requested, review budget left| G["Fix the findings, commit and push"]
    G --> C
    F -->|changes requested after the delta| R["Resolve or reject findings with reasons<br/>or fix in a replacement PR"]
    R -->|current head still has eligible review coverage| H
    F -->|approved| H["Take the PR out of draft and check readiness<br/><sub>loopzero ready --wait</sub>"]
    H -->|review coverage lost and budget allows another review| F
    H -->|ready| I["Merge or wait for the queue<br/><sub>loopzero merge --wait</sub>"]
    I -->|landed| K["Verify landing and clean up branch and worktree"]
```

Summarizes [The six steps](../core/CONTRACT.md#the-six-steps),
[Blockers](../core/CONTRACT.md#blockers) (review coverage, head movement and
readiness) and the [review budget](../core/CONTRACT.md#review-budget).
`loopzero ready` can admit the PR when it leaves draft on repositories with
auto-queue rules. For [loop-zero's queue](MERGIFY.md), `loopzero merge` requests
admission explicitly.

## 2. How many reviews a change gets

The PR owner is the session delivering the PR, or the coordinator once it
adopts it ([The six steps](../core/CONTRACT.md#the-six-steps)).

```mermaid
flowchart TD
    A["Primary review"] -->|approved| Z["Done: continue to ready"]
    A -->|fixes needed| B["Make all fixes in one batch"]
    B --> C["One review of only the new changes<br/><sub>delta</sub>"]
    C -->|approved| Z
    C -->|approved, blockers resolved, then a verified base merge conflicts| M["One extra review of verified base merges<br/><sub>conflict delta</sub>"]
    M -->|approved| Z
    M -->|findings require another change| E
    C -->|threads remain and this head has review coverage| D["PR owner resolves threads or rejects findings with reasons"]
    D -->|all blockers cleared| Z
    C -->|another reviewed fix needed| E["PR owner opens a replacement PR"]
    G["Reviewer gives no verdict:<br/>tool or login failure"] --> H["Costs nothing: try the next<br/>eligible model family"]
    H -->|none can run| J["Same-family review in a fresh session,<br/>disclosed on the PR"]
    J -->|none can run| I["PR owner restores a configured reviewer and retries the review,<br/>or reports the blocker to whoever commissioned the PR"]
```

See the contract's [review budget](../core/CONTRACT.md#review-budget).

## 3. Who does what

```mermaid
flowchart TD
    O["Human owner<br/><sub> gives orders to the decisions owner</sub>"] --> D["Decisions owner<br/><sub>resolves owner decisions within delegated authority</sub>"]
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
