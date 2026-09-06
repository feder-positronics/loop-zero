# loop-zero

A small, versioned agent workflow shared by repositories. This repository owns
the core; product repositories consume identical pinned snapshots and retain
their own commands, architecture, security and release policy.

The portable distribution is [`core/`](core/CONTRACT.md): four skills, one
handoff, thin Codex and Claude entry points, four conditional framework profiles
and a read-only pin check. There is no dispatcher, task runner or installer.

See [setup and revision updates](SETUP.md). Run the source tests with
`python3 -m unittest discover -s tests -v` (Python 3.11+ and Git).

A deposit is not evidence of product portability. That requires real changes in
two consumers on the same final core revision, both runtime entry points used,
and the repositories' ordinary validation and review evidence.
