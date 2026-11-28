# Working in loop-zero

- Follow the [delivery contract](core/CONTRACT.md).
- Use the relevant [delivery skill](core/skills/README.md).
- Before delegating, follow [model selection](docs/DELEGATION.md#resolve-the-model-before-delegation)
  and [briefs](docs/DELEGATION.md#briefs-and-ownership).
- Before committing changes to delivery interfaces (including commands, their
  options and configuration), state, trust boundaries, review/readiness or
  ownership, read the [architecture guide](docs/ARCHITECTURE.md). Edit it only
  when a boundary it describes moves. When `loopzero check` reports a watched
  change and the guide still holds, add a commit trailer
  `Architecture-Review: unchanged;` followed by the specific reason.
- Add [truthful model attribution](core/CONTRACT.md#review-budget) yourself;
  Claude does not add it automatically here.
