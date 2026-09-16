# Pinned IntelFlo skill reference

`expected.json` records SHA-256 digests of every file in the 23 migrated
governance skill directories from `feder-positronics/intelflo` at commit
`dbcafedac828d8454ce89a21e90228b568694e71`. The source was the consumer's
`.cursor/skills` tree, independently of the loop-zero renderer. Both that tree
and `.agents/skills` were clean at capture; the original byte comparison passed
against this revision. The manifest checks exact supporting-file sets as well
as content, including frontmatter.

`workflow.toml` freezes the IntelFlo rendering profile used by the original
comparison, including its explicit command values. Its core revision is the
existing synthetic test revision, not the consumer provenance revision.
Tests must not reconstruct this profile from current renderer defaults.
`consumer-owned/` contains small synthetic preservation fixtures for all twelve
consumer skill names, including a nested supporting file. They are deliberately
not copies of the product's skill implementations.

Default tests need no external checkout, network, or credentials. To additionally
compare a local consumer against the pinned manifest, set both variables:

```sh
LOOPZERO_INTELFLO_CHECKOUT=/path/to/intelflo \
LOOPZERO_INTELFLO_REVISION=dbcafedac828d8454ce89a21e90228b568694e71 \
python -m pytest tests/test_skills.py
```

The optional comparison requires a full commit SHA matching HEAD and clean
canonical/mirror trees (including untracked and ignored files). An incomplete
opt-in fails instead of silently skipping. It never updates the fixture.

For an intentional contract change, review the rendered change in an independently
pinned consumer commit first. Capture hashes from that clean consumer's
`.cursor/skills/<skill>/` files, update the provenance commit and frozen profile
when needed, then run both the default and explicit live comparisons. Do not
regenerate expected hashes from the renderer under test: that would erase the
independent regression oracle. Review removed/added file paths alongside hashes.
