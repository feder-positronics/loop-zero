"""Argument parsing shared by the console script and CLI module."""

from __future__ import annotations

import argparse
import io
import sys

WAIT_TIMEOUT = 1800.0


def _wait_seconds(value: str) -> float:
    from math import isfinite

    try:
        seconds = float(value)
        if isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected a finite, nonnegative number of seconds")


def _config_path(value: str):
    from pathlib import Path

    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopzero", description="One task, one branch, one draft PR, one review, one merge."
    )
    parser.add_argument(
        "--config", type=_config_path, default=None, help="path to workflow.toml (default: repo root)"
    )
    parser.add_argument("--version", action=_VersionAction, nargs=0, help="show program's version number and exit")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="create an owned worktree and branch for <slug>")
    start.add_argument("slug")
    sub.add_parser("check", help="run the configured checks in the sandbox")
    sub.add_parser("pr", help="push and open or refresh the draft PR")
    review = sub.add_parser("review", help="run native review and post one canonical review")
    review.add_argument(
        "--repost", action="store_true", help="post the saved review for HEAD without a model"
    )
    review.add_argument("--model", help="reviewer model override for a fresh review")
    review.add_argument("--effort", help="reviewer effort override for a fresh review")
    ready = sub.add_parser(
        "ready", help="compute readiness and mark the PR ready; --wait polls required checks"
    )
    merge = sub.add_parser(
        "merge", help="recheck readiness, merge and clean up; --wait waits for checks and landing"
    )
    for waiter, what in ((ready, "required checks on this head"), (merge, "required checks and the landed merge")):
        waiter.add_argument(
            "--wait", nargs="?", const=WAIT_TIMEOUT, type=_wait_seconds, metavar="SECONDS",
            help=f"wait for {what} (default {WAIT_TIMEOUT:.0f}s); exit 3 on timeout",
        )
    merge.add_argument(
        "--cancel", action="store_true", help="request removal from the configured Mergify queue"
    )
    resolve = sub.add_parser(
        "resolve", usage='%(prog)s [<finding-id> "<reply>"]',
        help="list open findings, or reply to one and resolve it",
    )
    resolve.add_argument("finding", nargs="?", help="finding id from the list (8 hex digits)")
    resolve.add_argument("reply", nargs="?", default="", help="what changed, e.g. the fix commit")
    sub.add_parser("status", help="show where this branch is in the sequence")
    return parser


def _version() -> str:
    """Package version plus the pinned source revision when installed from Git."""
    import json
    from importlib.metadata import distribution

    dist = distribution("loopzero")
    try:
        commit = json.loads(dist.read_text("direct_url.json") or "{}")["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        commit = "unknown revision (not installed from a Git URL)"
    return f"loopzero {dist.version} {commit}"


class _VersionAction(argparse.Action):
    def __call__(self, parser, _namespace, _values, _option_string=None):
        print(_version())
        parser.exit()


def parse_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    # Progress lines must precede the stderr verdict when both go to one file.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    args = parser.parse_args(argv)
    if args.command == "resolve" and args.finding is not None and not args.reply.strip():
        parser.error('expected resolve [<finding-id> "<reply>"] with a nonblank reply for a finding')
    if args.command == "review" and args.repost:
        conflicts = [f"--{name}" for name in ("model", "effort") if getattr(args, name) is not None]
        if conflicts:
            parser.error(f"review --repost cannot be combined with {' or '.join(conflicts)}")
    return args
