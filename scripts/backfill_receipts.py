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
    failed = False
    for clone in args.clones:
        try:
            clone = clone.resolve()
            config = config_mod.load(clone / 'workflow.toml')
            pr = cli._require_pr(config, cli.worktree.branch(clone), allow_merged=True)
            if pr.state != 'MERGED' or cli.worktree.head(clone) != pr.head_sha:
                raise cli.CliError('PR is not merged or local HEAD differs')
            if not list((clone / '.loopzero').glob('review-*.json')):
                continue
            print(f'{clone}: merged PR #{pr.number}, head {pr.head_sha}')
            if args.confirm:
                cli._cleanup(clone, config, pr)
                if clone.exists():
                    raise cli.CliError('archive or cleanup incomplete')
        except Exception as exc:  # noqa: BLE001 - manual batch must continue after any clone failure.
            print(f'retaining {clone}: {exc}')
            failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
