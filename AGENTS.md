# Working in loop-zero

- Follow the [delivery contract](core/CONTRACT.md).
- Use the relevant [delivery skill](core/skills/README.md).
- Follow [model selection and delegation](docs/DELEGATION.md) for bounded
  subagent work. Read the task repository's `models.toml` to resolve model and
  effort before delegation; select both explicitly when the runtime supports it.
- Before committing changes to delivery interfaces (including commands, their
  options and configuration), state, trust boundaries, review/readiness or
  ownership, read the [architecture guide](docs/ARCHITECTURE.md) and decide
  whether its account still holds. If not, update it in the same change; if so,
  add a commit trailer `Architecture-Review: unchanged;` followed by the specific
  reason. For watched files `loopzero check` fails without one of them.
