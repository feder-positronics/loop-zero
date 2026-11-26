"""Console entry point. Load delivery commands only after parsing."""

from loopzero._cli_args import build_parser, parse_args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(build_parser(), argv)
    from loopzero.cli import _run

    return _run(args, console=True)
