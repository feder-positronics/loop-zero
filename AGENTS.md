# Working in loop-zero

Follow the [delivery contract](core/CONTRACT.md) and use the relevant
[delivery skill](core/skills/README.md).

Use subagents for independent bounded work when delegation is useful, following
[model selection and delegation](docs/DELEGATION.md). Resolve the task's model
and effort by reading the task repository's `models.toml` before delegation; explicitly
select that model and effort when the runtime supports it. Categories are
starting points, and the parent owns integration and acceptance. Preserve
explicit user choices and runtime permission limits. Formal delivery review
remains the responsibility of `loopzero review`.

Read the [current architecture guide](docs/ARCHITECTURE.md) when changing delivery
interfaces, state or trust boundaries, or ownership. Review and update its account
in the same change, or record a reasoned `Architecture-Review: unchanged; ...`
commit trailer when it stays accurate. The guide describes the deterministic
local/CI check and the semantic review contributors and reviewers still own.
