# 2026-10-27 Codex startup-failure adjudication

Status: retired; no runtime targets or receipt pins remain in trusted eligibility code
(`src/loopzero/eligibility.py`). The original scope and recovery conditions moved
out of the delivery contract on 2026-10-30. The [architecture guide](../ARCHITECTURE.md#codex-review-runner-evidence)
describes current startup handling.

Recovery pins were retired by [#364](https://github.com/feder-positronics/loop-zero/pull/364),
the adjudication registries by [#555](https://github.com/feder-positronics/loop-zero/pull/555),
and the historical review fixtures were replaced with synthetic evidence by
[#612](https://github.com/feder-positronics/loop-zero/pull/612). Git history keeps the
retired policy text.
