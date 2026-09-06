# Codex entry point

This is an explicitly referenced instruction file, not an installed skill.
Read [the shared contract](../CONTRACT.md) and `workflow.toml` at the consumer
repository root, then open the relevant core skill and selected profiles.
Existing root/area instructions and native skills retain their local authority.

The consumer links this file from its existing `AGENTS.md`; preserve that file
and its current skill wiring. Check the active runtime's documented loading
behavior before adding automatic discovery. The core does not assume nested
vendored skills are automatically discovered.
