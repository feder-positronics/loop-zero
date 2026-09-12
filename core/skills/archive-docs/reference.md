# Archive Docs — {{package.product_name}} Reference

- Candidate manifest: `tmp/docs-archive-candidates.json`
- Lifecycle policy: [docs-lifecycle.yaml]({{package.docs_root}}/policies/docs-lifecycle.yaml)
- Historical-copy root: the selected manifest row's `tombstone_root` under
  [{{package.docs_dir}}/archive]({{package.docs_root}}/archive/)
- Metadata contract: [frontmatter template]({{package.docs_root}}/templates/frontmatter.md)

```bash
{{toolchain.commands.docs_archive_candidates}}
{{toolchain.python}} {{toolchain.scripts_dir}}/docs/batch_archive_ready.py
{{toolchain.commands.docs_verify}}
```

Run candidate generation immediately before selection. Do not infer eligibility
from filesystem age, status prose, or a previous manifest. The manifest exposes
`linked_from`, `review_notes`, and `archive_mode`. The helper refreshes it at
both mutation boundaries, archives only unblocked `batch` rows by default, and
regenerates existing managed indexes after the move.

After resolving every semantic note for a knowledge-bearing row, opt in by path:

```bash
{{toolchain.python}} {{toolchain.scripts_dir}}/docs/batch_archive_ready.py \
  --reviewed {{package.docs_dir}}/ops/incidents/example.md
```

## Per-File Evidence

```bash
source_path="{{package.docs_dir}}/path/to/source.md"
recovery_sha="$(git log -1 --diff-filter=AM --format='%H' -- "$source_path")"
test -n "$recovery_sha"
git cat-file -e "${recovery_sha}:${source_path}"
git show "${recovery_sha}:${source_path}" >/dev/null
```

Record only a full revision that contains the exact source path; a deletion
commit is not recovery evidence. Move the source unchanged to the configured
historical-copy root, then retain its original frontmatter followed by exactly
one line linking to that copy. The helper also appends an idempotent one-line
entry to the policy-selected digest. Verify the tombstone target resolves. Do
not rewrite backlinks or plain-text references; managed indexes are generated
projections owned by the helper.

## Event Documents

For a bug report or incident, resolve each manifest `review_notes` item. Confirm
that reusable prevention or operational knowledge is already in a living rule,
skill, guide, runbook, learning, or test, or record evidence that none exists.
Open follow-ups and event prose that remains the primary knowledge store block
archival even when the row otherwise appears ready.

Learnings and deprecated runbooks also use `manual_review`; first confirm their
guidance is encoded in living surfaces.

## Search and Cadence

Default repository search omits `{{package.docs_dir}}/archive/historical/**` to keep active-doc
results useful. Search recovery copies deliberately with:

```bash
rg --no-ignore "pattern" {{package.docs_dir}}/archive/historical
```

`{{toolchain.commands.metabolism_status}}` refreshes the manifest and reports the archive sweep
due when 20 rows are ready, the oldest ready row has waited 7 days beyond its
age threshold, or the 30-day cadence backstop expires. After a successful
terminal sweep, record it:

```bash
{{toolchain.python}} {{toolchain.scripts_dir}}/util/agent_event.py invoke --skill archive-docs
```

## Recovery

Keep the archive change uncommitted until validation finishes. On interruption
or failure, inspect the task diff and restore or repair only its source,
historical copy, and tombstone; do not reset unrelated work. The recovery SHA
restores the historical content, but it does not excuse a missing tombstone.
