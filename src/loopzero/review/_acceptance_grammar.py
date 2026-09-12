#!/usr/bin/env python3
"""Acceptance-command grammar for governed dispatch (#3944 decomposition).

Pure classification: shell segmentation, pnpm/pytest/uv/make/timeout wrapper
parsing, DB-lane recognition (fail-closed for serialization via
``acceptance_requires_database``; positive-only for capability grants via
``_strictly_requires_database``), and directory-operand extraction. No
subprocess execution, filesystem writes, or credential handling lives here.
This module must not import ``agent_dispatch`` or ``dispatch_acceptance``.
"""

import json
import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from contextvars import ContextVar

from ..config import Profile


_TOOLCHAIN: ContextVar[dict[str, object] | None] = ContextVar(
    "acceptance_grammar_toolchain", default=None
)
_DEFAULT_TOOLCHAIN: dict[str, object] = {}


def configure(profile: Profile) -> None:
    global _DEFAULT_TOOLCHAIN
    configured = dict(profile.toolchain)
    _DEFAULT_TOOLCHAIN = configured
    _TOOLCHAIN.set(configured)


def _toolchain() -> dict[str, object]:
    configured = _TOOLCHAIN.get()
    return configured if configured is not None else _DEFAULT_TOOLCHAIN


def _db_make_targets() -> frozenset[str]:
    return frozenset(str(value) for value in _toolchain().get("db_targets", ()))


def _db_lock_path() -> str:
    return str(_toolchain().get("db_lock", "/tmp/loopzero-testdb.lock"))


def pin_pnpm_commands(commands: Sequence[str]) -> list[str]:
    """Resolve pnpm through Corepack's nearest repository packageManager pin."""
    return [
        re.sub(
            r"(?<!corepack )(?<![A-Za-z0-9_.-])pnpm(?=\s|$)",
            "corepack pnpm",
            command,
        )
        for command in commands
    ]

PNPM_INVOKE_PATTERN = (
    r"(?<![A-Za-z0-9_.-])"
    r"(?:corepack\s+pnpm|(?:node\s+)?(?:\S*/)?pnpm(?:\.cjs)?)"
    r"(?![A-Za-z0-9_.-])"
)

PNPM_DIRECTORY_OPTIONS = frozenset({"-C", "--dir"})

PNPM_FILTER_OPTIONS = frozenset({"-F", "--filter"})

PNPM_OPTIONS_WITH_VALUE = PNPM_DIRECTORY_OPTIONS | PNPM_FILTER_OPTIONS

SHELL_BUILTINS = frozenset(
    {
        "cd",
        "echo",
        "exit",
        "false",
        "grep",
        "node",
        "rg",
        "test",
        "true",
    }
)


def _directory_operand(command: str) -> str | None:
    """Extract a command's declared ``cd``/``-C``/``--dir`` operand, if any."""
    match = re.search(r"(?:^|&&\s*|;\s*)cd\s+([^&;\s]+)\s*(?:&&|;)", command)
    if match:
        return match.group(1)
    match = re.search(
        rf"{PNPM_INVOKE_PATTERN}\s+(?:-C|--dir)(?:\s+|=)([^\s]+)", command
    )
    if match:
        return match.group(1)
    return None

def _escaping_directory_operand(command: str, worktree: Path) -> str | None:
    """Return the operand when it resolves outside the worktree, else None."""
    operand = _directory_operand(command)
    if operand is None:
        return None
    try:
        (worktree / operand).resolve().relative_to(worktree.resolve())
    except (OSError, RuntimeError, ValueError):
        return operand
    return None

def _pnpm_arguments(command: str) -> list[str]:
    """Extract a pnpm invocation's arguments without evaluating its shell text."""
    match = re.search(PNPM_INVOKE_PATTERN, command)
    if match is None:
        return []
    try:
        shell_tokens = shlex.split(command[match.end() :])
    except ValueError:
        return []
    arguments: list[str] = []
    for token in shell_tokens:
        if token in {"&&", "||", ";", "|"}:
            break
        arguments.append(token)
    return arguments

def _pnpm_command_arguments(command: str) -> list[str]:
    """Return pnpm arguments after global options and their values."""
    arguments = _pnpm_arguments(command)
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in PNPM_OPTIONS_WITH_VALUE:
            index += 2
        elif any(
            argument.startswith(f"{option}=") for option in PNPM_OPTIONS_WITH_VALUE
        ):
            index += 1
        elif argument.startswith("-"):
            index += 1
        else:
            break
    return arguments[index:]

def _pnpm_script_name(command: str) -> str | None:
    """Return the package script invoked by a pnpm command, if any."""
    arguments = _pnpm_command_arguments(command)
    if not arguments or arguments[0] == "exec":
        return None
    return (
        arguments[1] if arguments[0] == "run" and len(arguments) > 1 else arguments[0]
    )

def _pnpm_exec_executable(command: str) -> str | None:
    """Return the executable from a pnpm exec invocation, if any."""
    arguments = _pnpm_command_arguments(command)
    return arguments[1] if len(arguments) > 1 and arguments[0] == "exec" else None

def _pnpm_uses_filter(command: str) -> bool:
    """Return whether package selection cannot resolve to one package directory."""
    return any(
        argument in PNPM_FILTER_OPTIONS
        or any(argument.startswith(f"{option}=") for option in PNPM_FILTER_OPTIONS)
        for argument in _pnpm_arguments(command)
    )

def _script_executables(script: str) -> list[str]:
    """Find local package binaries referenced by a package script."""
    executables: list[str] = []
    for segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", script):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        index = 0
        while index < len(tokens) and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[index]):
            index += 1
        if index >= len(tokens):
            continue
        executable = tokens[index]
        if executable == "pnpm":
            try:
                executable = tokens[tokens.index("exec", index) + 1]
            except (ValueError, IndexError):
                continue
        if executable in SHELL_BUILTINS or "/" in executable:
            continue
        executables.append(executable)
    return list(dict.fromkeys(executables))

def _pnpm_script_executables(command: str, package_dir: Path) -> list[str]:
    """Resolve a normal pnpm package-script command to its local executables."""
    script_name = _pnpm_script_name(command)
    if script_name is None:
        return []
    package_path = package_dir / "package.json"
    try:
        package = json.loads(package_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(package, dict):
        return []
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return []
    script = scripts.get(script_name)
    return _script_executables(script) if isinstance(script, str) else []

DB_MAKE_TARGETS = frozenset()

SHELL_COMMAND_SEPARATOR_CHARS = frozenset(";&|\n")

PYTEST_TARGET_PATTERN = re.compile(r"(?:^|[\s/'\"])tests/(?:integration|e2e)(?:/|$)")

PYTEST_POSITIONAL_TARGET_PATTERN = re.compile(r"(?:^|/)tests(?:/|$)")

PYTEST_ROOT_TARGET_PATTERN = re.compile(r"(?:^|/)tests/?$")

SHELL_ASSIGNMENT_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

ENV_OPTIONS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})

ENV_FLAG_OPTIONS = frozenset({"-i", "--ignore-environment", "-0", "--null"})

UV_RUN_OPTIONS_WITH_VALUE = frozenset(
    {"-w", "--with", "--group", "--directory", "--project", "--python"}
)

UV_RUN_FLAG_OPTIONS = frozenset(
    {
        "-q",
        "--quiet",
        "--frozen",
        "--locked",
        "--no-sync",
        "--offline",
        "--all-extras",
        "--all-groups",
        "--no-dev",
    }
)

PYTEST_OPTIONS_WITH_VALUE = frozenset(
    {
        "-W",
        "-k",
        "-m",
        "-n",
        "-p",
        "--junit-xml",
        "--junitxml",
        "--rootdir",
    }
)

PYTEST_FLAG_OPTIONS = frozenset(
    {
        "-V",
        "-h",
        "-l",
        "-q",
        "-s",
        "-v",
        "-x",
        "--cache-clear",
        "--collect-only",
        "--collect-in-virtualenv",
        "--co",
        "--continue-on-collection-errors",
        "--disable-warnings",
        "--disable-plugin-autoload",
        "--disable-pytest-warnings",
        "--doctest-continue-on-failure",
        "--doctest-ignore-import-errors",
        "--doctest-modules",
        "--exitfirst",
        "--failed-first",
        "--ff",
        "--fixtures",
        "--fixtures-per-test",
        "--force-short-summary",
        "--full-trace",
        "--funcargs",
        "--help",
        "--keep-duplicates",
        "--last-failed",
        "--lf",
        "--markers",
        "--new-first",
        "--nf",
        "--no-fold-skipped",
        "--no-header",
        "--no-showlocals",
        "--no-summary",
        "--noconftest",
        "--pdb",
        "--pyargs",
        "--quiet",
        "--runxfail",
        "--setup-only",
        "--setup-plan",
        "--setup-show",
        "--showlocals",
        "--spec",
        "--stepwise",
        "--stepwise-reset",
        "--stepwise-skip",
        "--strict",
        "--strict-config",
        "--strict-markers",
        "--sw",
        "--sw-reset",
        "--sw-skip",
        "--trace",
        "--trace-config",
        "--verbose",
        "--version",
        "--xfail-tb",
    }
)

TIMEOUT_LONG_OPTIONS_WITH_VALUE = frozenset({"--kill-after", "--signal"})

TIMEOUT_LONG_FLAG_OPTIONS = frozenset(
    {"--foreground", "--preserve-status", "--verbose", "--help", "--version"}
)

TIMEOUT_SHORT_OPTIONS_WITH_VALUE = frozenset({"k", "s"})

TIMEOUT_SHORT_FLAG_OPTIONS = frozenset({"f", "p", "v", "h", "V"})

TIMEOUT_DURATION_PATTERN = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)[smhd]?")

FLOCK_EXECUTABLES = frozenset({"flock", "/usr/bin/flock"})

FLOCK_TEST_DB_LOCK_PATH = "/tmp/loopzero-testdb.lock"

FLOCK_TIMEOUT_PATTERN = re.compile(r"[1-9][0-9]*")

SHELL_WRAPPER_NAMES = frozenset({"bash", "sh"})

SHELL_OPTIONS_WITH_VALUE = frozenset({"-o", "-O", "--rcfile", "--init-file"})


type _ShellCommandOperand = tuple[Literal["wrapper", "malformed", "ordinary"], str]

def _is_pytest_no_value_flag(argument: str) -> bool:
    """Recognize listed pytest flags and valid clustered/repeated short flags."""
    if argument in PYTEST_FLAG_OPTIONS:
        return True
    if not argument.startswith("-") or argument.startswith("--") or "=" in argument:
        return False
    chars = argument[1:]
    return bool(chars) and all(f"-{char}" in PYTEST_FLAG_OPTIONS for char in chars)

def _acceptance_shell_tokens(command: str) -> list[str] | None:
    """Tokenize an acceptance command without treating comments as commands."""
    command_with_newline_sentinels = command.replace("\n", "\n;")
    lexer = shlex.shlex(
        command_with_newline_sentinels, posix=True, punctuation_chars=";&|\n"
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError:
        return None

def _acceptance_command_segments(command: str) -> list[list[str]] | None:
    segments: list[list[str]] = []
    current: list[str] = []
    tokens = _acceptance_shell_tokens(command)
    if tokens is None:
        return None
    for token in tokens:
        is_separator = bool(token) and all(
            character in SHELL_COMMAND_SEPARATOR_CHARS for character in token
        )
        if is_separator:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments

def _strip_environment_prefix(segment: Sequence[str]) -> list[str]:
    """Skip shell assignments and the common ``env`` command prefix."""
    tokens = list(segment)
    while tokens and SHELL_ASSIGNMENT_PATTERN.fullmatch(tokens[0]):
        tokens.pop(0)
    if tokens and Path(tokens[0]).name == "env":
        tokens.pop(0)
        while tokens:
            token = tokens[0]
            if SHELL_ASSIGNMENT_PATTERN.fullmatch(token):
                tokens.pop(0)
            elif token == "--":
                tokens.pop(0)
                break
            elif token in ENV_OPTIONS_WITH_VALUE:
                tokens.pop(0)
                if not tokens:
                    return []
                tokens.pop(0)
            elif token in ENV_FLAG_OPTIONS or any(
                token.startswith(prefix) and len(token) > len(prefix)
                for prefix in ("-u", "-C")
            ):
                tokens.pop(0)
            elif token.startswith("--unset=") or token.startswith("--chdir="):
                tokens.pop(0)
            else:
                break
    return tokens

def _direct_pytest_arguments(tokens: Sequence[str]) -> list[str] | None:
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable in {"pytest", "py.test"}:
        return list(tokens[1:])
    if executable in {"python", "python3", "pypy", "pypy3"} and len(tokens) >= 3:
        if tokens[1] == "-m" and tokens[2] in {"pytest", "py.test"}:
            return list(tokens[3:])
    return None

def _uv_run_command_tokens(tokens: Sequence[str]) -> list[str] | None:
    if len(tokens) < 3 or Path(tokens[0]).name != "uv" or tokens[1] != "run":
        return None
    runner_tokens = list(tokens[2:])
    while runner_tokens and runner_tokens[0].startswith("-"):
        option = runner_tokens.pop(0)
        if option == "--":
            return runner_tokens or None
        option_name, separator, value = option.partition("=")
        if option_name in UV_RUN_FLAG_OPTIONS:
            if separator:
                return None
            continue
        if option_name in UV_RUN_OPTIONS_WITH_VALUE:
            if separator:
                if (
                    not value
                    or option_name.startswith("-")
                    and not option_name.startswith("--")
                ):
                    return None
                continue
            if not runner_tokens:
                return None
            runner_tokens.pop(0)
            continue
        if option.startswith("-") and not option.startswith("--"):
            chars = option[1:]
            if chars and all(f"-{char}" in UV_RUN_FLAG_OPTIONS for char in chars):
                continue
        return None
    return runner_tokens or None

def _pytest_arguments(segment: Sequence[str]) -> list[str] | None:
    """Return pytest arguments for one shell command segment, if it invokes it."""
    tokens = _strip_environment_prefix(segment)
    direct = _direct_pytest_arguments(tokens)
    if direct is not None:
        return direct
    runner_tokens = _uv_run_command_tokens(tokens)
    return (
        _direct_pytest_arguments(runner_tokens) if runner_tokens is not None else None
    )

def _is_source_only_test_path_command(segment: Sequence[str]) -> bool:
    """Recognize explicit source-analysis commands whose path is not executed."""
    tokens = _strip_environment_prefix(segment)
    runner_tokens = _uv_run_command_tokens(tokens)
    command = runner_tokens if runner_tokens is not None else tokens
    if not command:
        return False
    executable = Path(command[0]).name
    return executable == "ruff" or (
        executable in {"python", "python3", "pypy", "pypy3"}
        and len(command) >= 3
        and command[1:3] == ["-m", "ruff"]
    )

def _pytest_targets_database(arguments: Sequence[str]) -> bool:
    """Return whether pytest may collect integration or e2e tests.

    Explicit unit-only positionals stay DB-less. Missing positionals or
    unknown option grammar are DB-required because default collection can
    include integration tests (serialization fails closed)."""
    return _pytest_database_decision(arguments) is not False

def _pytest_database_decision(arguments: Sequence[str]) -> bool | None:
    """True/False on positive recognition; None when option grammar is
    unknown, so capability decisions can fail closed the other way."""
    remaining = list(arguments)
    positional_only = False
    saw_test_target = False
    while remaining:
        argument = remaining.pop(0)
        option, separator, _value = argument.partition("=")
        if not positional_only and argument == "--":
            positional_only = True
            continue
        if not positional_only and separator and argument.startswith("-"):
            continue
        if not positional_only and argument in PYTEST_OPTIONS_WITH_VALUE:
            if not remaining:
                return None
            remaining.pop(0)
            continue
        if not positional_only and _is_pytest_no_value_flag(argument):
            continue
        if not positional_only and argument.startswith("-"):
            return None
        if PYTEST_TARGET_PATTERN.search(argument):
            return True
        if PYTEST_ROOT_TARGET_PATTERN.search(argument):
            return True
        if PYTEST_POSITIONAL_TARGET_PATTERN.search(argument):
            saw_test_target = True
    return not saw_test_target

def _make_targets_database(segment: Sequence[str]) -> bool:
    tokens = _strip_environment_prefix(segment)
    if not tokens or Path(tokens[0]).name != "make":
        return False
    return any(argument in _db_make_targets() for argument in tokens[1:])

def _timeout_wrapped_tokens(tokens: Sequence[str]) -> list[str] | None:
    """Return the command wrapped by timeout, or None when malformed."""
    if not tokens or Path(tokens[0]).name != "timeout":
        return None
    rest = list(tokens[1:])
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option == "--":
            break
        option_name, separator, _value = option.partition("=")
        if option.startswith("--"):
            all_long_options = (
                TIMEOUT_LONG_OPTIONS_WITH_VALUE | TIMEOUT_LONG_FLAG_OPTIONS
            )
            if option_name in all_long_options:
                resolved_option = option_name
            else:
                matches = {
                    candidate
                    for candidate in all_long_options
                    if candidate.startswith(option_name)
                }
                if len(matches) != 1:
                    return None
                resolved_option = matches.pop()
            if resolved_option in TIMEOUT_LONG_FLAG_OPTIONS:
                if separator:
                    return None
                continue
            if not separator:
                if not rest:
                    return None
                value = rest.pop(0)
            else:
                value = _value
            if not value or (
                resolved_option == "--kill-after"
                and not TIMEOUT_DURATION_PATTERN.fullmatch(value)
            ):
                return None
            continue
        if separator:
            return None
        chars = option[1:]
        if not chars:
            return None
        for index, char in enumerate(chars):
            if char in TIMEOUT_SHORT_FLAG_OPTIONS:
                continue
            if char not in TIMEOUT_SHORT_OPTIONS_WITH_VALUE:
                return None
            value = chars[index + 1 :]
            if not value:
                if not rest:
                    return None
                value = rest.pop(0)
            if value.startswith("=") or (
                char == "k" and not TIMEOUT_DURATION_PATTERN.fullmatch(value)
            ):
                return None
            break
    if not rest or not TIMEOUT_DURATION_PATTERN.fullmatch(rest[0]):
        return None
    rest.pop(0)
    return rest or None


def _flock_wrapped_tokens(tokens: Sequence[str]) -> list[str] | None:
    """Return the command from the one supported test-DB flock grammar.

    The grammar is deliberately closed: only the canonical executable names,
    ``-w`` spelling, bounded integer timeout, and shared lock path are
    accepted.  In particular, flock's descriptor and ``-c`` forms are not
    command wrappers here.
    """
    if not tokens or tokens[0] not in FLOCK_EXECUTABLES:
        return None
    if len(tokens) < 5 or tokens[1] != "-w":
        return None
    timeout = tokens[2]
    if not FLOCK_TIMEOUT_PATTERN.fullmatch(timeout):
        return None
    try:
        timeout_seconds = int(timeout)
    except ValueError:
        return None
    if not 1 <= timeout_seconds <= 1800:
        return None
    if tokens[3] != _db_lock_path():
        return None
    wrapped = list(tokens[4:])
    if not wrapped or not wrapped[0] or wrapped[0].startswith("-"):
        return None
    return wrapped


def _shell_command_operand(tokens: Sequence[str]) -> _ShellCommandOperand:
    """Classify a bash/sh segment as ``-c`` wrapper, malformed, or ordinary."""
    if not tokens or Path(tokens[0]).name not in SHELL_WRAPPER_NAMES:
        return ("ordinary", "")
    index = 1
    saw_command_option = False
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            if not saw_command_option:
                return ("ordinary", "")
            if index + 1 >= len(tokens):
                return ("malformed", "")
            return ("wrapper", tokens[index + 1])
        if not token.startswith("-"):
            return ("wrapper", token) if saw_command_option else ("ordinary", "")
        if token.startswith("--"):
            option_name, separator, _value = token.partition("=")
            if option_name in SHELL_OPTIONS_WITH_VALUE and not separator:
                if index + 1 >= len(tokens):
                    return ("malformed", "")
                index += 2
                continue
            index += 1
            continue
        chars = token[1:]
        saw_command_option = saw_command_option or "c" in chars
        value_count = sum(f"-{char}" in SHELL_OPTIONS_WITH_VALUE for char in chars)
        next_operand = index + 1 + value_count
        if next_operand > len(tokens):
            return ("malformed", "")
        index = next_operand
    return ("malformed", "") if saw_command_option else ("ordinary", "")

def _wrapped_targets_database(segment: Sequence[str]) -> bool:
    """Fail closed when a supported wrapper carries a DB-backed test lane."""
    tokens = _strip_environment_prefix(segment)
    if not tokens:
        return False
    runner_tokens = _uv_run_command_tokens(tokens)
    if runner_tokens is not None:
        return _segment_targets_database(runner_tokens)
    executable = Path(tokens[0]).name
    if executable == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        return True
    if executable == "timeout":
        wrapped = _timeout_wrapped_tokens(tokens)
        if wrapped is None:
            return True
        return _segment_targets_database(wrapped)
    if tokens[0] in FLOCK_EXECUTABLES:
        wrapped = _flock_wrapped_tokens(tokens)
        if wrapped is None:
            return True
        return _segment_targets_database(wrapped)
    if executable in SHELL_WRAPPER_NAMES:
        kind, command = _shell_command_operand(tokens)
        if kind == "ordinary":
            return False
        if kind == "malformed":
            return True
        inner_segments = _acceptance_command_segments(command)
        if inner_segments is None:
            return True
        return any(_segment_targets_database(inner) for inner in inner_segments)
    return False

def _segment_targets_database(segment: Sequence[str]) -> bool:
    """Return whether one parsed segment invokes a supported DB-backed lane."""
    arguments = _pytest_arguments(segment)
    if arguments is not None:
        return _pytest_targets_database(arguments)
    return _make_targets_database(segment) or _wrapped_targets_database(segment)

def acceptance_requires_database(command: str) -> bool:
    """Return whether an acceptance command exercises DB-backed test lanes.

    Classification recognizes only explicit supported command and wrapper
    grammar. Malformed or unknown pytest option grammar fails closed. Explicit
    ``db_acceptance_commands`` remains authoritative for DB-bound lanes outside
    that grammar.
    """
    segments = _acceptance_command_segments(command)
    if segments is None:
        return True
    for segment in segments:
        arguments = _pytest_arguments(segment)
        if arguments is not None:
            if _pytest_targets_database(arguments):
                return True
            continue
        if _segment_targets_database(segment):
            return True
        if _is_source_only_test_path_command(segment):
            continue
        if any(PYTEST_TARGET_PATTERN.search(token) for token in segment):
            return True
    return False

def _strictly_targets_database(segment: Sequence[str]) -> bool:
    """Positive-only DB recognition for capability grants: every fail-closed
    branch of the serialization classifier (unknown wrapper grammar, malformed
    shell operands, bare path-looking tokens) returns False here instead."""
    arguments = _pytest_arguments(segment)
    if arguments is not None:
        return _pytest_database_decision(arguments) is True
    if _make_targets_database(segment):
        return True
    tokens = _strip_environment_prefix(segment)
    if not tokens:
        return False
    runner_tokens = _uv_run_command_tokens(tokens)
    if runner_tokens is not None:
        return _strictly_targets_database(runner_tokens)
    executable = Path(tokens[0]).name
    if executable == "timeout":
        wrapped = _timeout_wrapped_tokens(tokens)
        return wrapped is not None and _strictly_targets_database(wrapped)
    if tokens[0] in FLOCK_EXECUTABLES:
        wrapped = _flock_wrapped_tokens(tokens)
        return wrapped is not None and _strictly_targets_database(wrapped)
    if executable in SHELL_WRAPPER_NAMES:
        kind, command = _shell_command_operand(tokens)
        if kind != "wrapper":
            return False
        inner_segments = _acceptance_command_segments(command)
        return inner_segments is not None and any(
            _strictly_targets_database(inner) for inner in inner_segments
        )
    return False

def _strictly_requires_database(command: str) -> bool:
    """Anchored-grammar DB recognition only: unparseable grammar and the
    loose any-token fallback never count — those still serialize as
    DB-bound, but they must not earn any capability."""
    segments = _acceptance_command_segments(command)
    if segments is None:
        return False
    return any(_strictly_targets_database(segment) for segment in segments)
