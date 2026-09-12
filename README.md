# loop-zero

A small, versioned agent workflow shared by repositories. This repository owns
the core; product repositories consume identical pinned snapshots and retain
their own commands, architecture, security and release policy.

The portable distribution has two parts pinned by one commit SHA. The vendored
[`core/`](core/CONTRACT.md) snapshot holds the contract, four skills, one
handoff, thin Codex and Claude entry points, four conditional framework
profiles and read-only pin and check-policy tools. The `loopzero` Python
package, installed from the same SHA, holds the executable mechanisms: the
worktree lease, sandboxed validation children, the signed evidence ledger,
detached jobs, runner adapters, the dispatcher and the delivery state machine,
plus a generator for consumer wiring. The core ships no scheduler service,
dashboard, cloud control plane or installer beyond that generator.

See [setup and revision updates](SETUP.md). Run the source tests with
`.venv/bin/python -m pytest -q` (Python 3.12+ and Git).

A deposit is not evidence of product portability. That requires real changes in
two consumers on the same final core revision, both runtime entry points used,
and the repositories' ordinary validation and review evidence.

Version 0.2.1 fixes release-metadata environment checks and adds check classes,
evidence-backed overrides, and an offline check-policy report. Consumers must
bump their snapshot and full commit pin to the reviewed 0.2.1 merge commit as
described in SETUP.md. The
single durable debt list is [KNOWN-GAPS.md](KNOWN-GAPS.md), curated at closeout.
CI runs the tests and `python3 core/tools/status.py --known-gaps KNOWN-GAPS.md
--check-child-env`; run `git diff --check` before freezing a review candidate.
There are no configured lint, type or complexity ratchets in this source package;
consumers must run every applicable gate from their own contract.
