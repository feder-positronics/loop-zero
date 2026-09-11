"""``loopzero`` command line.

Subcommands present in this release: ``init``, ``sync``, ``status``,
``policy lint``, ``doctor``, ``checks``. Later releases add ``worktree``,
``job``, ``ledger``, ``dispatch``, ``review``, ``delivery`` and ``evidence``
as their modules land. Exit code 0 means the command produced its result; a
report command's exit code never asserts that a policy passed unless the
command's help says so.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    CONTRACT_ID,
    WORKFLOW_FILE,
    ConfigError,
    Profile,
    effective_hooks,
    load_profile,
)
from . import sync as sync_module

TOOLS = ("git", "bwrap", "openssl", "gh")

INIT_TEMPLATE = """# Commands run from the repository root; narrow checks to the affected area.
profiles = []

[core]
repository = "https://github.com/feder-positronics/loop-zero"
revision = "{revision}"
path = "vendor/loop-zero"

[package]
env_prefix = "LOOPZERO"
audit_root = ".audit"
state_root = "~/.local/state/loopzero"
contract = "{contract}"
epoch = 1
sandbox = "bwrap"

[checks]
required = []
advisory = []
scheduled = []

[paths]
constraints = "AGENTS.md"

[commands]
setup = []
check_fast = []
check_integration = "not-applicable: fill in or state why there is no integration surface"

[hooks]
"""


def _run_tool(profile: Profile, name: str, argv: list[str]) -> int:
    """Run a snapshot tool as a child; the tools parse ``sys.argv`` themselves."""
    path = profile.snapshot_dir / "tools" / f"{name}.py"
    if not path.is_file():
        raise ConfigError([f"{path}: snapshot tool missing; deposit the core snapshot first"])
    proc = subprocess.run([sys.executable, "-I", str(path), *argv])
    return proc.returncode


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    target = root / WORKFLOW_FILE
    if target.exists() and not args.force:
        print(f"{target} exists; use --force to overwrite", file=sys.stderr)
        return 1
    revision = args.revision or "0" * 40
    target.write_text(INIT_TEMPLATE.format(revision=revision, contract=CONTRACT_ID), encoding="utf-8")
    print(f"wrote {target}; set [core].revision to the inspected full SHA and fill [checks]")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.root))
    if args.check:
        drift = sync_module.check(profile)
        if drift:
            print("drift:", file=sys.stderr)
            for path in drift:
                print(f"  {path}", file=sys.stderr)
            print("run `loopzero sync` and commit the result", file=sys.stderr)
            return 1
        print("wiring matches workflow.toml")
        return 0
    changed = sync_module.write(profile)
    for path in changed:
        print(f"updated {path}")
    if not changed:
        print("no changes")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.root))
    snapshot = profile.snapshot_version()
    ok = True
    if snapshot is None:
        print(f"snapshot: missing at {profile.snapshot_dir}", file=sys.stderr)
        ok = False
    elif snapshot != __version__:
        print(f"version mismatch: package {__version__} vs snapshot {snapshot}", file=sys.stderr)
        ok = False
    else:
        print(f"package {__version__} matches snapshot {snapshot}")
    if args.source:
        rc = _run_tool(profile, "status", ["--consumer", str(profile.root), "--source", args.source])
        ok = ok and rc == 0
    print("pass" if ok else "fail")
    return 0 if ok else 1


def cmd_policy_lint(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        profile = load_profile(root)
    except ConfigError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        print("fail")
        return 1
    problems: list[str] = []
    if args.base:
        try:
            hooks = effective_hooks(profile, args.base)
        except ConfigError as exc:
            problems.extend(exc.problems)
            hooks = {}
        for name, commands in hooks.items():
            for command in commands:
                executable = command.split()[0]
                if "/" not in executable and shutil.which(executable) is None:
                    problems.append(f"[hooks].{name}: executable {executable!r} not on PATH")
    for name, alias in profile.aliases.items():
        if alias.runner in ("claude", "codex", "cursor"):
            binary = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}[alias.runner]
            if shutil.which(binary) is None:
                problems.append(f"[routing.aliases].{name}: runtime {binary!r} not installed")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("fail")
        return 1
    print("pass")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    for tool in TOOLS:
        path = shutil.which(tool)
        print(f"{tool}: {path or 'missing'}")
        ok = ok and path is not None
    if sys.platform != "linux":
        print("platform: unsupported (Linux only)")
        ok = False
    print(f"python: {sys.version.split()[0]}")
    print("pass" if ok else "fail")
    return 0 if ok else 1


def cmd_checks(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.root))
    argv = ["checks", "--workflow", str(profile.root / WORKFLOW_FILE)]
    if args.results:
        argv += ["--results", args.results]
    return _run_tool(profile, "checks", argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopzero", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"loopzero {__version__}")
    parser.add_argument("--root", default=".", help="consumer repository root (default: .)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter workflow.toml")
    p.add_argument("--revision", help="full commit SHA of the inspected core")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("sync", help="render consumer wiring from workflow.toml")
    p.add_argument("--check", action="store_true", help="report drift, write nothing; exit 1 on drift")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="package and snapshot versions must match; optional pin check")
    p.add_argument("--source", help="local source checkout for the byte-level pin check")
    p.set_defaults(func=cmd_status)

    policy = sub.add_parser("policy", help="policy commands").add_subparsers(dest="policy_command", required=True)
    p = policy.add_parser("lint", help="validate workflow.toml; with --base also resolve hooks from the base")
    p.add_argument("--base", help="base ref whose [hooks] govern privileged hooks, e.g. origin/main")
    p.set_defaults(func=cmd_policy_lint)

    p = sub.add_parser("doctor", help="report required host tools")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("checks", help="read-only check-policy report from the snapshot tool")
    p.add_argument("--results", help="JSON results file keyed by exact check name")
    p.set_defaults(func=cmd_checks)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        print("UNVERIFIED", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
