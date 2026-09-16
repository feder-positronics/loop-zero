# loop-zero

A versioned workflow core for repository-based coding agents: shared operating
rules, sandboxed execution, review evidence, and delivery mechanisms. Product
repositories keep their own commands, architecture, security, and release policy.

loop-zero makes that workflow explicit and reusable across consumers. It is
intended for repositories that need more than a collection of agent prompts:
validation and review results must remain tied to the work they actually cover.

## How it fits together

Consumers pin two artifacts to the **same full Git commit SHA**:

| Part | Responsibility |
| --- | --- |
| [`core/`](core/CONTRACT.md), vendored into the consumer | Shared contract, governance and methodology skills, runtime entry points, framework profiles, handoff template, and pin/check-policy tools |
| [`loopzero`](src/loopzero/), installed as a Python package | Worktree leases, sandboxed validation, signed evidence, detached jobs, runner adapters, dispatch, delivery, and consumer wiring generation |
| Consumer-owned configuration | Commands, paths, required checks, runtime wiring, and product-specific policy |

The executable package separates [kernel mechanisms](src/loopzero/kernel/README.md),
review policy, [runner contracts](src/loopzero/runners/README.md), and delivery.
See the [architecture record](docs/design/2026-09-11-executable-core-architecture.md)
for the boundaries and their rationale.

## Adopt it

Use Python **3.12+** and Git. Sandboxed execution also requires the host facilities
documented by the [kernel](src/loopzero/kernel/README.md); CI uses Linux and
bubblewrap. Runtime integrations have their own optional dependencies.

Follow [setup and revision updates](SETUP.md) to:

1. Select and inspect a full source commit.
2. Deposit its `core/` snapshot and install the Python package from that same commit.
3. Define the consumer's `workflow.toml`, generate its wiring, and connect its runtimes.
4. Verify the pin and run the consumer's required checks.

A branch or tag is not a consumer pin. Package metadata comes from
[`core/VERSION`](core/VERSION); dependency and runtime pins live in
[`pyproject.toml`](pyproject.toml), [`uv.lock`](uv.lock), and the relevant workflows.

## Develop and validate

From a source checkout with [uv](https://docs.astral.sh/uv/) installed:

```sh
uv sync --locked --group dev --extra code-health
uv run python -m pytest -q
uv run python core/tools/status.py --known-gaps KNOWN-GAPS.md --check-child-env
git diff --check
```

The [deterministic CI workflow](.github/workflows/checks.yml) runs validation in a
sandbox on Python 3.12 and 3.14 and checks installed artifacts. The commands above
are local checks; they do not reproduce CI's filesystem isolation. Changes to
authority mechanisms must also satisfy the
[authority review policy](docs/design/authority-review-bar.md).

The optional [code-health collector](docs/code-health.md) reports size,
complexity, and duplication. Its comparisons are advisory. Consumers retain
responsibility for every applicable gate in their own contract.

## Runtime support and limits

Runner integrations cover Claude, Codex, and Cursor. Capabilities are explicit:
a runtime that cannot satisfy a required contract produces an unsupported or
unavailable outcome. In particular, the pinned Cursor CLI cannot enforce
`output_schema`, so schema-dependent live success and restart/resume scenarios
are recorded as unsupported.

Ordinary tests use deterministic fixtures and replay. Live provider behavior is
validated separately by [nightly conformance](docs/nightly-runtime-conformance.md),
with pinned runtimes, credential isolation, cost accounting, and explicit release
acceptance criteria. See [credential operations](docs/nightly-conformance.md)
before running that suite.

A deposited snapshot does not establish product portability. That requires real
changes in two consumers on the same final core revision, both runtime entry
points used, and their ordinary validation and review evidence. Current debt is
curated in [KNOWN-GAPS.md](KNOWN-GAPS.md).

The core ships no scheduler service, dashboard, or cloud control plane. Its
consumer wiring generator is not a replacement for repository-owned setup and
release decisions.

## Documentation

Start with the [documentation map](docs/README.md) for adoption, development,
operations, design decisions, and proposals. The
[strategic review](docs/strategic-review-2026-09-16.md) records the SOLID,
simplification, and documentation findings, with implementation evidence and
remaining stages.

License: MIT (declared in [package metadata](pyproject.toml)).
