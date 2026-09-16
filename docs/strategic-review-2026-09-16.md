Strategic review of loop-zero — 2026-09-16

Reviewed source: `1cc1f76eb995a5f30fafa23524a606bfce3662ee`.
This is an assessment and proposed sequence, not an accepted architecture
decision or a new debt ledger. The findings describe the baseline; the local
implementation follow-up is recorded at the end.

**Assessment.** Loop-zero has valuable security and evidence mechanisms, but
its extraction into a reusable package has preserved too many configuration
conventions, policy implementations, and compatibility paths. The strongest
opportunity is to give each policy one owner and make configuration explicit.
Splitting large files without changing these relationships would have limited
benefit. Three small offline probes reproduced disagreements between existing
interfaces; those should lead the work.

The package's own committed-source collector completed a size-only inventory:
107 Python files and 63,688 physical lines under `src`. Kernel accounts for
20,178 lines, review 17,287, runners 12,003, and delivery including its docs
helpers 5,806. The shell job launcher adds 2,773 lines outside that Python
inventory. These are scope measurements, not a quality score. The architecture
record's 6,000-line budget applies to a particular Cutover B rebuild, not the
entire package; comparing that budget directly with 63,688 would be misleading.

| Priority | Finding | Evidence strength | Main design concern |
| --- | --- | --- | --- |
| High | Configuration can disagree across modules | Reproduced in a fresh process | Dependency inversion; temporal coupling |
| High | Publication thread readers enforce different rules | Reproduced with malformed input | One policy has two implementations |
| High | Configured review sections disagree with runner validation | Reproduced with a valid profile | Contract consistency; policy ownership |
| Next | Review authority crosses the kernel boundary in both directions | Source inspection | Dependency inversion; single responsibility |
| Next | Consumer policy remains embedded in delivery mechanisms | Source inspection | Portability; separation of policy and mechanism |
| Later | Runner files combine transport and credential lifecycles | Source inspection | Single responsibility; capability contracts |
| Incremental | Configuration parsing and package cleanup | Source inspection | Local simplicity; documentation accuracy |

1. **Make configuration coherent before extending it.**

   [review/__init__.py](../src/loopzero/review/__init__.py) eagerly imports the
   mechanisms at line 9, then `configure()` replaces kernel settings at line 65.
   [kernel/settings.py](../src/loopzero/kernel/settings.py), line 120, rebinds
   its global object. Numerous mechanisms use `from .settings import settings`;
   [gitscope.py](../src/loopzero/kernel/gitscope.py), lines 11 and 34, additionally
   derives `DISPATCH_DIR` at import time. Updating the settings module cannot
   update those already captured references or derived constants.

   A fresh process importing `loopzero.review`, validating an ACME profile with
   `audit_root = ".private-audit"`, and calling `review.configure(profile)` gave:

   ```text
   configured: ACME .private-audit
   gitscope: LOOPZERO .audit/dispatch
   sandbox: LOOPZERO .audit
   same_settings_object: False
   ```

   This establishes inconsistent configuration, not a demonstrated sandbox
   escape. The kernel README requires configuration before mechanism imports;
   callers following that order avoid the initial mismatch. However, importing
   the review package itself crosses that boundary before its configuration
   entry point can run. The API should enforce a coherent lifecycle rather
   than leave callers to understand its import graph.

   Start by defining one supported initialization sequence and ensuring ordinary
   imports do not capture consumer configuration. For the lasting design,
   construct services with explicit settings and immutable dependencies;
   resolve paths through those dependencies. Keep the existing one-consumer
   process restriction until there is a real need for more. Replacing globals
   with a mutable singleton alone would leave concurrency and lifecycle
   ambiguity. A dynamic accessor is only a migration step, and derived constants
   still need attention.

   Verify a nondefault namespace and audit root at actual kernel consumers in a
   fresh subprocess, including a worker thread. The existing configuration tests
   check the settings holder and several review readers, but miss this split.

2. **Consolidate publication thread validation.**

   [publish.py](../src/loopzero/delivery/publish.py), line 724, implements a second
   thread reader for `pr=<number>`. Its filter at line 752 only treats a dictionary
   with `isResolved is False` as unresolved. Malformed nodes are silently ignored;
   a nonboolean `hasNextPage` also terminates pagination, and repeated cursors
   are not detected.

   The [shared reader](../src/loopzero/delivery/_publish_threads.py), lines 78–110,
   validates node fields and pagination and rejects repeated cursors. With
   `nodes=[{}]` and `hasNextPage=False`, the PR-number interface returned normally;
   the shared URL interface raised `GateError` for an invalid thread. This is
   an evidence-validation defect in the alternate interface. The main
   `publish()` flow currently uses the stricter URL interface; the probe does
   not establish a bypass of that ordinary flow.

   Use one connection parser and pagination implementation. Adapt the two
   transport envelopes into it. Preserve strict rejection of malformed evidence,
   explicit resolution of outdated threads, GraphQL errors, and cursor cycles.
   A small transport adapter is sufficient; a general GitHub framework is not
   needed. Run the same malformed-page cases through both public interfaces.

3. **Give review-section policy one owner.**

   [config.py](../src/loopzero/config.py), line 972, accepts consumer-defined
   section identifiers. [review/chain.py](../src/loopzero/review/chain.py),
   lines 33–49 and 158, derives requirements from that configured profile.
   [runners/_review_schema.py](../src/loopzero/runners/_review_schema.py),
   lines 11 and 105, independently hardcodes `code` plus conditional `security`.
   [runners/contract.py](../src/loopzero/runners/contract.py), line 605, calls
   that second validator during governed output-schema construction.

   A profile containing `required_sections = ["code", "architecture", "security"]`
   passed configuration validation. For no security-trigger paths, the chain
   accepted `["code", "architecture"]`; the runner validator rejected it.
   This can block schema construction for an otherwise accepted consumer policy.
   The two `review_sections_schema()` functions are also identical AST copies.

   Resolve policy once in the trusted review layer, bind the resulting section
   requirements to the task, and pass that validated contract to schema building
   and result validation. Keep boundary validation in both places, but have it
   validate the same contract rather than independently choose policy. Preserve
   mandatory security-section rules. If customization is intentionally unsupported,
   reject it in configuration instead; silently accepting it is the current
   problem. Given the existing profile API, coherent customization is the more
   compatible direction.

4. **Separate authority facts, projections, and admission decisions.**

   [authority_projection.py](../src/loopzero/kernel/authority_projection.py)
   imports review routing inside `_legacy_review_generations()` at line 1788
   and review authority inside `slot_state()` at line 2577. Those review modules
   depend back on the kernel. [review_state.py](../src/loopzero/kernel/review_state.py)
   contains record types and a 649-line `resolve_generation()` starting at line
   1104 that calls projections. The projection module imports those record types
   back. Local imports postpone loading; they do not remove the design cycle.

   [seams.py](../src/loopzero/kernel/seams.py) already provides dependency
   injection, but through a global string-to-`Callable` registry with untyped
   forwarding functions. [review/authority.py](../src/loopzero/review/authority.py),
   line 1684, configures it during import. This mixes explicit construction with
   implicit registration and direct reverse imports.

   A useful target is a dependency-free home for authority record types and
   intrinsic outcome semantics, authenticated storage/projection services, and
   a review decision layer consuming those projections. Use small typed
   dependency objects or protocols for the operations each caller actually
   needs. Prefer ordinary functions for pure transformations; classes are not
   required everywhere. Preserve the closed registry of recognized authority
   families and rejection of unknown governed records.

   [admission.py](../src/loopzero/review/admission.py), line 548, is the clearest
   refactoring target after the concrete defects: `admit_review()` spans 963
   lines and combines source checks, Git-backed scope derivation, generation
   resolution, trust obligations, retry reconciliation, and slot reservation.
   Its result union (`Carry`, `Reserved`, `Blocked`, `NonVerdictReviewLaunch`) is
   already a good boundary. Retain it and separate evidence gathering from
   decisions over validated inputs. Keep lock scope and atomic append/reserve
   guarantees explicit; a prettier function must not introduce stale decisions.

   Isolate legacy record translation behind an authenticated compatibility
   boundary before considering deletion. Compare native, legacy, compacted,
   replayed, and interrupted histories against the current verdict and slot
   semantics. Removing legacy history without migration evidence is unsafe.

5. **Complete policy portability in a narrow vertical slice.**

   Package naming has advanced further than policy extraction. For example,
   [delivery/_visual_evidence.py](../src/loopzero/delivery/_visual_evidence.py),
   lines 46–47, assumes `nextjs-frontend/app`, `components`, and `__tests__`.
   [delivery/_publish_body.py](../src/loopzero/delivery/_publish_body.py), line 32,
   fixes the checker path to `scripts/util/pr_body_check.py`.
   [delivery/_publish_findings.py](../src/loopzero/delivery/_publish_findings.py),
   line 31, reads `.audit/skill-runs`, and publication contains multiple `.audit`
   defaults despite the configurable audit root. Configured classifiers elsewhere
   do not automatically make these paths configurable.

   Introduce only the policy values consumed by one complete review-to-publication
   path: audit locations, body requirements/checker identity, and visual-evidence
   triggers. Carry approved policy through existing settings rather than adding
   scattered callback parameters. Keep first-consumer defaults in an explicit
   compatibility profile where needed. Test a synthetic consumer with different
   directory names and audit root. Actual portability still requires ordinary
   delivery evidence from two consumers on the same revision; fixtures alone
   cannot establish it.

6. **Split runner responsibilities without flattening vendor differences.**

   Claude and Codex each occupy roughly 2,700 lines, while the shared bridge has
   2,789. The vendor modules contain parsers, readiness/selection, execution,
   result normalization, credential validation, renewal locks, refresh, and
   snapshot export. [claude.py](../src/loopzero/runners/claude.py) makes the split
   visible: its adapter starts at line 906, credential errors at line 1895, and
   renewal machinery later. These responsibilities change for different reasons.

   Separate host credential lifecycle, vendor transport, and protocol parsing.
   Keep pure shared mechanisms such as cost accounting and process containment
   shared. Do not force provider refresh and fallback semantics into a large
   inheritance hierarchy. The existing three-method
   [RuntimeAdapter](../src/loopzero/runners/contract.py) protocol is reasonably
   small and worth preserving.

   For substitution, make supported request capabilities explicit: schema
   enforcement, resume, permission evidence, and budget controls. The repository
   already documents Cursor's unsupported schema-dependent conformance scenarios.
   A common Python signature does not establish interchangeable behavior. Check
   required capabilities before a paid launch, and preserve explicit unsupported
   results rather than relaxing completion criteria. This review did not launch
   providers or verify current external SDK behavior.

   Treat the known broken Codex CLI backup path as a bounded product decision:
   repair and exercise it if a supported caller needs it; otherwise explicitly
   deprecate it and remove its compatibility surface after caller checks. Adding
   another fallback would enlarge an already complicated selection contract.

7. **Use small cleanup work to support the boundaries above.**

   `config.validate()` spans 713 lines. Extract validators by existing TOML
   section and preserve aggregate diagnostics and cross-section checks. There
   is no demonstrated need to add a configuration framework to the dependency-free
   runtime. Review heavily populated optional request objects when changing their
   owning behavior; introduce distinct states only where they eliminate invalid
   combinations or clarify which evidence a step requires.

   Remove obsolete `sys.path.insert()` operations from package-only delivery
   helpers after checking their callers; several execute during ordinary imports.
   Consolidate wrapper branches that duplicate policy rather than merely adapting
   a signature. Update the kernel README's claim that composition-root registration
   remains `TODO(A4)`: registration already exists in `review.configure()` and
   `review.authority`. Duplicate comments and old source-era terminology are
   cleanup opportunities, but much lower priority than the contract disagreements.

   Keep intentional duplication where importing shared code would cross a trust
   boundary. The copied Git configuration predicate in
   [closeout.py](../src/loopzero/delivery/closeout.py), line 401, explicitly runs
   before source pinning and has parity tests against
   [git_config_security.py](../src/loopzero/kernel/git_config_security.py).
   Unlike the review-section and thread-reader duplication, this copy has a
   documented reason to exist. Likewise retain revalidation across process and
   authentication boundaries; reuse policy, not an unchecked earlier verdict.

The SOLID emphasis should be **single responsibility and dependency inversion**.
Interface segregation matters for the broad implicit seam registry. Liskov
substitution is chiefly about honest runtime capabilities. Open/closed design
should make consumer variation explicit, while security allowlists and recognized
record families remain deliberately closed. A plugin framework, universal base
adapter, or generic event engine would add machinery without addressing the
observed defects.

The proposed sequence is deliberately incremental:

| Order | Deliverable | Evidence that the change helped |
| --- | --- | --- |
| 1 | Correct configuration lifecycle, thread-reader parity, and section-contract agreement in separate changes | The three reproductions become regression checks; existing focused suites stay green |
| 2 | One owner for section policy and one validated thread reader; remove obsolete import side effects | Policy changes no longer require synchronized implementations |
| 3 | One consumer-neutral review/publication path | Different audit/frontend paths work through the whole path; no hidden first-consumer lookup |
| 4 | Extract authority types and explicit projection dependencies, then simplify admission | Remove the named runtime dependency cycles; preserve verdict, reservation, compaction, and recovery behavior |
| 5 | Separate credential lifecycle from transports; retire unsupported compatibility paths with evidence | A credential change no longer requires unrelated parser/transport edits; supported conformance remains intact |

Measure changed policy locations, removed dependency cycles, recovered operations,
and repeated configuration/delivery failures. Use the existing code-health
collector as supporting evidence. Avoid a repository-wide line limit or a
combined “sloppiness” score: both could reward moving complexity rather than
removing it. The existing delivery-blockage strategy and code-health plan already
cover recovery and measurement; these recommendations should feed those efforts
and the single curated debt process rather than create another workflow.

**Validation and limits.** The size collector completed against the exact commit
above. AST inspection identified large functions, internal import relationships,
and exact function clones; type-only imports were not treated as runtime cycles
in the findings. Three fresh-process/in-memory probes reproduced the behaviors
reported above without network calls or external mutations. The existing
configuration, runner-schema, and publication test files passed together:
22 tests, using `uv run --no-project --offline --with pytest --with jsonschema
python -m pytest -q -p no:cacheprovider` followed by those three test paths.
An initial minimal test environment lacked `jsonschema`; the successful rerun
included it. The full suite, live providers, deployed consumers, concurrency
under production load, and a complete security audit were outside this review.


**Implementation follow-up.** The first correctness tranche is now implemented
locally. Review configuration loads mechanisms only after selecting kernel
settings and rejects conflicting late initialization before mutation. Tests run
configuration scenarios in fresh processes instead of resetting many module globals.
Both publication interfaces use one strict page reader. Review-section selection,
task validation, and schema fragments share `loopzero.review_contract`; consumers
pass approved custom policy through `governed_result_schema(configured_sections=...)`.
The unconfigured legacy security requirement is preserved. Lazy compatibility
exports and an isolated bridge package root preserve direct delivery imports and
file-path bridge execution without adding consumer directories to `sys.path`.

The full ordinary suite ran with source/Git metadata read-only, the account home
isolated, and networking disabled: 3,075 passed, 259 skipped, and 58 subtests passed.
Optional jscpd was unavailable in the offline environment. A final security-only
custom-policy compatibility case was then added, confirmed failing, and repaired;
all 62 owning tests passed after that correction. The dependency-free wheel
includes the shared module and passed direct delivery-import and custom-schema
probes. Repository status and whitespace checks passed. No live providers were
launched, consumer pins changed, or changes merged. The larger authority,
portability, and credential/transport decompositions remain subsequent stages.
