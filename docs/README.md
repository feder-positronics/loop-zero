# Docs

Start with [ARCHITECTURE.md](ARCHITECTURE.md) for the current architecture,
ownership and documentation upkeep routine, then the top-level files for delivery
and setup.

- [../README.md](../README.md) — what loop-zero is, the six commands, adoption.
- [../SETUP.md](../SETUP.md) — prerequisites, install from a SHA, connect agent
  skills, configure API keys, verify local setup, and first run.
- [../workflow.example.toml](../workflow.example.toml) — annotated config.
- [../core/CONTRACT.md](../core/CONTRACT.md) — the delivery rules.
- [../core/CREDENTIALS.md](../core/CREDENTIALS.md) — default API-key lookup,
  local storage, Bitwarden, rotation, and CI/production boundaries.
- [../core/HANDOFF.md](../core/HANDOFF.md) — task file and PR body template.
- [../core/skills/](../core/skills) — 19 core skills; start with `plan`,
  `implement`, `work-issue`, or `write-design-doc`.
- [../integrations/](../integrations/) — optional vendor integration skills.
- [REWRITE-SPEC.md](REWRITE-SPEC.md) — module map and live contributor references.
- [design/2026-09-24-delivery-system-architecture.md](design/2026-09-24-delivery-system-architecture.md) —
  dated consumer evidence and accepted principles; its gaps and measurements
  describe September 24, not the current backlog.
- [DELEGATION.md](DELEGATION.md) — model selection, flexible delegation,
  and verification through the delivery contract.
- [../models.toml](../models.toml) — current model/effort preferences and category defaults.
