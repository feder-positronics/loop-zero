# Resolve Findings — {{package.product_name}} Reference

Repo-specific bindings for the portable workflow in [SKILL.md](SKILL.md).

## Staleness Re-Verification

Saved audit reports live alongside dated artifact directories. Compare the report's
`mtime` with `{{package.audit_root}}/<today>/` (or the equivalent audit output dir). If the audit
directory is newer, re-read the cheap artifact checks before trusting the report:

- `docs_audit.txt`
- `docs_verify.txt`
- `docs_lifecycle_check.txt`
- `docs_archive_candidates.txt`
- `learnings_status.txt`
- `madge.txt`
- `eslint.txt`

Spot-check the findings the report lists against their current artifact state.
For code-review findings, staleness means the diff moved — re-check the finding's
file/line against the current branch instead.
