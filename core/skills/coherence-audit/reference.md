# Coherence Audit — Reference

## Composition census

Producer→consumer edges, per capability (service/route/model cluster):

```bash
rg -l "<ServiceOrSymbol>" {{toolchain.backend_dir}}/app -g '!**/tests/**'
```

- A capability's consumers = non-test files importing/calling it outside its
  own module. Zero consumers → built-but-unwired.
- Tool/API surfaces: compare what is advertised (manifests, routers, UI
  actions) against what is reachable/handled — the claims-manifest C1 pattern.
- Frontend caller census: grep the generated SDK symbol case-insensitively and
  exclude `{{toolchain.frontend_dir}}/app/openapi-client/` (the definition is not a caller).

## UI pattern census

```bash
# status-indication divergence
rg --files {{toolchain.frontend_dir}}/components | rg '(Badge|badge|Pill|pill|Status).*\.tsx$'
# overlay divergence
rg -l 'Dialog|Sheet|Drawer|Popover' {{toolchain.frontend_dir}}/components -g '*.tsx'
# placeholders / halves
rg -n -i 'coming soon|placeholder|not implemented' {{toolchain.frontend_dir}}/app -g '*.tsx'
```

Compare current counts against the [component catalog]; each bespoke variant of
a catalog primitive is a consolidation proposal, not an auto-fix.

[component catalog]: {{package.docs_root}}/components/catalog.md

## Value-vs-weight scoring

Weight = LOC owned + models + routes + scheduled tasks (rough counts are fine).
Apply [D-21 prototype evidence]({{package.rules_root}}/design.mdc#north-star): during
the prototype phase, execute the named user scenario and judge its result;
usage counts, adoption rates, and zero-use readings cannot settle value or
justify a cut. Replace usage-triggered open questions with executable scenarios.

Value evidence tiers for this phase: executed scenario evidence > persona-job
mapping > author's intent only. A feature whose best evidence is "author's
intent only" and whose weight is high leads the cut-list as an owner decision,
not an automatic removal. Outside the prototype phase, assess usage evidence
with its population and limitations; do not assume it outranks scenario proof.

## Report skeleton

```markdown
# Coherence report — <scope> — <date>
Verdict: <one paragraph>
## Wire-list (build these compositions)   — ranked, each: what · why (persona job) · effort
## Cut-list (owner decisions)             — ranked, each: what · weight · best value evidence · re-open trigger
## Consolidations (UI/pattern)            — variant count → target primitive
## Routed proposals and decisions
```

## Dossier skeleton

Single-capability scope, under the [explanation contract]'s register.

```markdown
# <Capability> — dossier — <date>
Verdict: <earns its place | earns it conditionally | cut candidate> — one paragraph
## What it is for          — the user and business problem, before any mechanism
## How it works today      — the real path end to end, named plainly
## Who it serves           — personas and named scenarios, with coverage gaps
## How well it works       — name the value evidence tier reached, per the scoring above
## Strengths and weaknesses — failure modes, overlaps, dependencies
## What it costs           — weight per the scoring above
## If we changed it        — simplify · merge · replace · remove, each with its consequence
## Strategy fit and scale  — Vision layer, and whether the shape holds at 10x
```

A dossier whose verdict outruns its evidence tier is worse than no dossier.

[explanation contract]: ../explain/SKILL.md#explanation-contract
