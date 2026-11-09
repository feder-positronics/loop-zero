"""Run by hand: uv run python scripts/backfill_receipts.py [--confirm] CLONE ...

Defaults to listing merged clones; --confirm archives receipts and removes them.
"""

import argparse
from pathlib import Path

from loopzero import cli
from loopzero import config as config_mod


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('clones', type=Path, nargs='+')
    args = parser.parse_args(argv)
    for clone in args.clones:
        clone = clone.resolve()
        config = config_mod.load(clone / 'workflow.toml')
        pr, _ = cli._pr_readiness(clone, config, allow_merged=True)
        if pr.state != 'MERGED':
            print(f'retaining {clone}: PR is not merged')
            continue
        if not list((clone / '.loopzero').glob('review-*.json')):
            continue
        print(f'{clone}: merged PR #{pr.number}, head {pr.head_sha}')
        if args.confirm:
            cli._cleanup(clone, config, pr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
