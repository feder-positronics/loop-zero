# IntelFlo code-health adoption

This is a tested consumer adapter prepared in loop-zero. It is **not installed
in IntelFlo**. IntelFlo PR #4419 owns the package cutover and overlapping pin,
dependency, Makefile, and generated-skill changes. Apply this integration in a
subsequent owned consumer change after that cutover, or incorporate it through
that PR's existing owner. Do not start a competing pin migration.

## Dependencies and policy

Pin the reviewed loop-zero commit containing this adapter and collector through
IntelFlo's normal package/core adoption. Keep its existing Ruff 0.5.7 requirement.
Add the `code-health-duplication` extra to the same pinned loopzero dependency;
this adds jscpd 5.2.0 without asking the resolver to upgrade Ruff. Refresh the
consumer lockfile through its serialized dependency workflow. Do not install
the default `code-health` extra there: it selects Ruff 0.16.7.

Copy `code_health.py` into consumer-owned `scripts/audit/code_health.py` and
review its explicit roots, exclusions, and test classification. It uses backend
app/tests and frontend app/components/lib/top-level tests. It excludes generated
code, the OpenAPI client, fixtures, dependencies, and backend maintenance scripts.
Those exclusions remain visible in evidence. Both PR snapshots use one policy
and one analyzer version. The existing Guardian command and thresholds remain
consumer-owned; source inspection reconciles any existing C901 findings.

## Health invocation

After `cursor_audit.sh` initializes/populates the current run's `status.json`,
invoke this adapter once in `make health`, before the final status summary:

```sh
fastapi_backend/.venv/bin/python scripts/audit/code_health.py \
  --root "$PWD" --head "$HEALTH_HEAD_SHA" --target all \
  --audit-dir "$AUDIT_DIR" --status "$AUDIT_DIR/status.json"
```

Supply the exact full SHA already recorded by that health run. Map `python`
and `next` health modes to the adapter's corresponding targets. Do not invoke
it for filesystem-only or specialized dead-code/coverage modes. The health
runner must serialize status writers and propagate a nonzero adapter exit;
never hide a failure with `|| true`. Missing prerequisites and collection errors
produce invalid evidence. If status identity changes or a file cannot be written,
the command fails, and the caller must retain that failure.

The adapter preserves other collectors and writes
`collectors.<target>.code_structure` in the existing status file, retaining its
run ID and source SHA. Candidates alone do not fail the command and never set
`threshold_failed`. `health_collector_status.py` already treats an invalid entry
as a failed health collection. No new status format or finding queue is needed.

Adopt shared audit references through the reviewed core pin and normal sync;
do not hand-edit generated skills or the vendored snapshot.

## Advisory PR invocation

```sh
fastapi_backend/.venv/bin/python scripts/audit/code_health.py \
  --root "$SOURCE_CHECKOUT" --base "$BASE_SHA" --head "$HEAD_SHA" \
  --target all --audit-dir "$PR_ARTIFACT_DIR"
```

The workflow must install the approved package/tool versions and execute this
adapter from trusted base policy. A candidate checkout supplies Git objects
only: do not execute its workflow, adapter, dependency metadata, or package
installation. Fetch the explicit commits into the source checkout and use the
consumer's validation containment. Record this adapter's actual exit status
(its in-process collector record derives status from validated completion),
retain incomplete reports, and upload JSON/Markdown through the existing PR
artifact path without commit, publication, or write-token authority.

Add the exact job name to `[checks].advisory`, not required contexts. Show an
unavailable collection as unavailable, even though it is non-required. The
report is evidence, not automatic findings; reconcile existing same-source
Guardian findings before routing new work. Validate this workflow through the
consumer's existing CI/security review before enabling it.

## Validation and rollout boundary

Loop-zero tests exercise source/policy admission, stale/malformed evidence,
failure propagation, preservation of existing status entries, generated-client
exclusion, and colocated tests. The native Ruff 0.5.7 and jscpd 5.2.0 path is also
checked against a read-only IntelFlo commit. This does not establish completed
consumer adoption, production CI operation, useful precision over ten PRs, or
second-consumer portability.
