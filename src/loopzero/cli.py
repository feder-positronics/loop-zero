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
import json
import os
import re
import shlex
import shutil
import stat
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
    resolve_base,
)
from . import gates
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


DEFAULT_EXECUTABLE_PATH = (Path("/usr/bin"), Path("/bin"))
SHELL_METACHARACTERS = re.compile(r"&&|\|\||[;&|<>`\r\n]|\$\(")


def _allowed_path(entries: list[str]) -> tuple[Path, ...]:
    allowed = list(DEFAULT_EXECUTABLE_PATH)
    problems: list[str] = []
    for entry in entries:
        path = Path(entry)
        if not path.is_absolute():
            problems.append(f"--path-entry {entry!r}: must be an absolute directory")
        elif not path.is_dir():
            problems.append(f"--path-entry {entry!r}: directory is unavailable")
        else:
            allowed.append(path)
    if problems:
        raise ConfigError(problems)
    return tuple(allowed)


def _regular_executable(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


def _repository_executable(root: Path, token: str) -> bool:
    candidate = root / token
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        current = root.resolve(strict=True)
        for part in Path(token).parts:
            if part in ("", "."):
                continue
            if part == "..":
                return False
            current = current / part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return False
        return stat.S_ISREG(os.lstat(candidate).st_mode) and os.access(candidate, os.X_OK)
    except (OSError, ValueError):
        return False


def _resolve_executable(root: Path, token: str, allowed: tuple[Path, ...]) -> Path | None:
    if "/" in token:
        path = Path(token)
        if not path.is_absolute():
            return (root / path).resolve() if _repository_executable(root, token) else None
        try:
            parent = path.parent.resolve(strict=True)
        except OSError:
            return None
        if any(parent == entry.resolve() for entry in allowed) and _regular_executable(path):
            return path.resolve()
        return None
    for directory in allowed:
        candidate = directory / token
        if _regular_executable(candidate):
            return candidate.resolve()
    return None


def _lint_hook_commands(
    profile: Profile, hooks: dict[str, tuple[str, ...]], allowed: tuple[Path, ...]
) -> list[str]:
    problems: list[str] = []
    for name, commands in hooks.items():
        for command in commands:
            where = f"[hooks].{name}"
            if SHELL_METACHARACTERS.search(command):
                problems.append(f"{where}: shell metacharacters are not allowed: {command!r}")
                continue
            try:
                argv = shlex.split(command, posix=True)
            except ValueError as exc:
                problems.append(f"{where}: invalid command quoting: {exc}")
                continue
            if not argv:
                problems.append(f"{where}: command has no executable")
            elif _resolve_executable(profile.root, argv[0], allowed) is None:
                problems.append(
                    f"{where}: executable {argv[0]!r} is not a regular executable in the allowed PATH"
                )
    return problems


def _run_source_status(profile: Profile, source_arg: str) -> subprocess.CompletedProcess[str]:
    try:
        source = Path(source_arg).resolve(strict=True)
        tool = source / "core" / "tools" / "status.py"
        if stat.S_ISLNK(os.lstat(tool).st_mode) or not stat.S_ISREG(os.lstat(tool).st_mode):
            raise OSError("verifier is not a regular file")
    except OSError as exc:
        raise ConfigError([f"trusted source verifier unavailable: {exc}"]) from None
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(tool),
            "--consumer",
            str(profile.root),
            "--source",
            str(source),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )


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
    if snapshot is None:
        print(f"version equality: unavailable; snapshot VERSION missing at {profile.snapshot_dir}")
    elif snapshot != __version__:
        print(f"version equality: different; package {__version__}, snapshot {snapshot}")
    else:
        print(f"version equality: equal; package and snapshot are {snapshot}")
    if not args.source:
        print("informational only; use --source for byte-level pin verification")
        return 0
    verifier = _run_source_status(profile, args.source)
    if verifier.returncode != 0:
        if verifier.stderr:
            print(verifier.stderr.rstrip(), file=sys.stderr)
        print("fail")
        return 1
    if snapshot is None or snapshot != __version__:
        print("fail")
        return 1
    print("pass")
    return 0


def cmd_policy_lint(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        profile = load_profile(root)
    except ConfigError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        print("fail")
        return 1
    if not args.no_hooks and not (args.base or args.base_ref):
        print("UNVERIFIED: hook-aware policy lint requires --base or --base-ref", file=sys.stderr)
        return 1

    allowed = _allowed_path(args.path_entry)
    problems: list[str] = []
    base_sha: str | None = None
    if not args.no_hooks:
        try:
            base_sha = resolve_base(profile.root, base=args.base, base_ref=args.base_ref)
            if args.base_ref:
                print(f"base: {base_sha}")
            hooks = effective_hooks(profile, base_sha)
        except ConfigError as exc:
            problems.extend(exc.problems)
            hooks = {}
        problems.extend(_lint_hook_commands(profile, hooks, allowed))
    for name, alias in profile.aliases.items():
        if alias.runner in ("claude", "codex", "cursor"):
            binary = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}[alias.runner]
            if _resolve_executable(profile.root, binary, allowed) is None:
                problems.append(f"[routing.aliases].{name}: runtime {binary!r} not in the allowed PATH")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("fail")
        return 1
    if args.no_hooks:
        print("policy is valid; hooks were not checked (--no-hooks)")
    else:
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
    try:
        results = json.loads(Path(args.results).read_text(encoding="utf-8")) if args.results else {}
        rows = gates.classify(profile.raw["checks"], results)
    except (OSError, ValueError, TypeError, RecursionError, KeyError) as exc:
        raise ConfigError([f"checks report: {exc}"]) from None
    print(json.dumps(rows, indent=2))
    return 0


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

    p = sub.add_parser("status", help="report version equality; --source performs a trusted pin check")
    p.add_argument("--source", help="local source checkout for the byte-level pin check")
    p.set_defaults(func=cmd_status)

    policy = sub.add_parser("policy", help="policy commands").add_subparsers(dest="policy_command", required=True)
    p = policy.add_parser("lint", help="validate workflow.toml and its base-governed hooks")
    base = p.add_mutually_exclusive_group()
    base.add_argument("--base", help="trusted full 40-character base commit SHA")
    base.add_argument("--base-ref", help="trusted base ref to resolve and print as a commit SHA")
    base.add_argument("--no-hooks", action="store_true", help="explicitly skip all hook checks")
    p.add_argument(
        "--path-entry",
        action="append",
        default=[],
        help="additional absolute executable directory in the lint allowlist",
    )
    p.set_defaults(func=cmd_policy_lint)

    p = sub.add_parser("doctor", help="report required host tools")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("checks", help="read-only in-package check-policy report")
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
