# 2026-10-27 Codex startup-failure adjudication

Status: retired; no runtime targets or receipt pins remain in trusted eligibility code
(`src/loopzero/eligibility.py`). The original scope and recovery conditions moved
out of the delivery contract on 2026-10-30. The [architecture guide](../ARCHITECTURE.md#codex-review-runner-evidence)
describes current startup handling.

- [#364](https://github.com/feder-positronics/loop-zero/pull/364) retired the last recovery pin.
- [#555](https://github.com/feder-positronics/loop-zero/pull/555) retired the adjudication registries.
- [#612](https://github.com/feder-positronics/loop-zero/pull/612) replaced the historical review fixtures with synthetic evidence.
