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

Version 0.2.0 adds PR-scoped review, validation-child containment requirements,
exact-head delivery evidence, and deterministic environment/debt checks. The
single durable debt list is [KNOWN-GAPS.md](KNOWN-GAPS.md), curated at closeout.
CI runs the tests and `python3 core/tools/status.py --known-gaps KNOWN-GAPS.md
--check-child-env`; run `git diff --check` before freezing a review candidate.
There are no configured lint, type or complexity ratchets in this source package;
consumers must run every applicable gate from their own contract.
