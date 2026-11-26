"""Console entry point. Load delivery commands only after parsing."""

from loopzero._cli_args import build_parser, parse_args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(build_parser(), argv)
    from loopzero.cli import _run

    return _exit_status(_run(args))


def _exit_status(code: int) -> int:
    if code < 0:
        import signal
        if -code not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(-code, signal.SIG_DFL)
        signal.raise_signal(-code)
    return code
