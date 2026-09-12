---
name: refine-code
description: >-
  Simplify recently touched backend or frontend code without intentional
  behavior, API, data, or test-contract change. Use after a bounded edit or
  before review. For evidenced dead-code removal use `remove-dead-code`.
---

# Refine Code

Make the user-named or current-session diff easier to read and maintain while
preserving its observable contract.

## Contracts

- Standalone mutation owns a worktree; nested work inherits the outer mutating
  workflow under the [parallel-agents rule]({{package.rules_root}}/parallel-agents.mdc).
- Apply the [Review Gate]({{package.rules_root}}/review-gate.mdc), the touched scope's
  [backend]({{package.rules_root}}/be.mdc) or [frontend]({{package.rules_root}}/fe.mdc) rule, and the
  validation commands in [AGENTS.md]({{package.constraints_file}}).
- Keep collector-classified `symbol_cleanup` inside an otherwise-live file in
  this skill; route an evidenced whole-file/module removal to
  [`remove-dead-code`](../remove-dead-code/SKILL.md), a stable module split to
  [`decompose-module`](../decompose-module/SKILL.md), a broad structural redesign
  to [`write-design-doc`](../write-design-doc/SKILL.md), and new behavior or a
  known-cause backend defect to [`implement-backend`](../implement-backend/SKILL.md) or
  [`implement-frontend`](../implement-frontend/SKILL.md), a concrete UI defect to
  [`fix-ui-bug`](../fix-ui-bug/SKILL.md), an unclear failure to
  [`debug`](../debug/SKILL.md), and review-only work to
  [`code-review`](../code-review/SKILL.md).

## Refinement Contract

- Scope defaults to files changed in the current logical task unless the user
  names a broader boundary. Do not sweep unrelated modules.
- Prefer clearer names and control flow, fewer redundant intermediates, and
  consolidation that reuses an established local pattern. Remove comments or
  live-file cleanup symbols only when their lack of runtime/tooling effect is
  proven; do not infer whole-file/module deadness here.
- Treat changes to concurrency, exception timing, transactions, ordering,
  query predicates, persistence, user isolation, serialization, public imports,
  API shape, and test assertions as semantic until equivalence is demonstrated.
  If the cleanup requires a behavior change,
  new abstraction boundary, test weakening, or uncertain deletion, stop and
  route it rather than disguising it as refinement.

## Evidence and Done

- Describe the concrete clarity gain and any edit that could look behavioral;
  provide evidence for observational equivalence across success and relevant
  failure/cancellation paths.
- Run the narrowest relevant lint, type, and focused test lanes for the touched
  scope. Done means those checks pass, public and behavioral contracts are
  unchanged, and no unrelated cleanup or compatibility layer was introduced.
- A no-behavior refactor of `.tsx` under `app/` or `components/` still draws
  the advisory visual-evidence warning (`fe.mdc` § Blocking Rules). Since
  observational equivalence is the skill's own exit criterion, the warning is
  expected and needs no bypass footer or manufactured screenshot — but if you
  cannot honestly claim equivalence, the change is not a `refine-code` change.
