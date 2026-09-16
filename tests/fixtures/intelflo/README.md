# IntelFlo publication facade

`pr_body_check.py` is the actual rendered consumer facade from IntelFlo commit
`dbcafedac828d8454ce89a21e90228b568694e71`, path
`scripts/util/pr_body_check.py`. Keep it verbatim: the regression must exercise
its real basename-based dispatch and package bootstrap, not a simplified stand-in.

The fixture contains no product data or runtime credentials. Tests install the
candidate loopzero package into a disposable interpreter and use synthetic PR
bodies. They never access the live consumer checkout or its toolchain.
