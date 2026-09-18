"""The single subprocess helper. Every external command goes through `run`."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .types import LoopZeroError

TAIL_LINES = 40


class ToolMissing(LoopZeroError):
    """The executable named by argv[0] could not be found on PATH."""


class ProcTimeout(LoopZeroError):
    """The command did not finish within the timeout."""


@dataclass(frozen=True)
class Completed:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def tail(text: str, lines: int = TAIL_LINES) -> str:
    """Return the last `lines` lines of `text`, without a trailing newline."""
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def build_env(
    env_allowlist: Sequence[str], extra_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Copy only allowlisted variables from the current environment, then apply extras."""
    env = {key: os.environ[key] for key in env_allowlist if key in os.environ}
    if extra_env:
        env.update(extra_env)
    return env


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    env_allowlist: Sequence[str],
    extra_env: Mapping[str, str] | None = None,
    timeout: float,
    merge_output: bool = False,
    input: str | None = None,
) -> Completed:
    """Run `argv` (never through a shell) with an environment stripped to the allowlist.

    `merge_output=True` sends stderr into stdout so interleaving is preserved.
    `input` is written to the child's stdin; without it stdin is /dev/null.
    Nonzero exit is reported in `Completed.exit_code`; callers decide whether to raise.
    """
    argv = [str(part) for part in argv]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=build_env(env_allowlist, extra_env),
            **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolMissing(f"{argv[0]}: executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        raise ProcTimeout(
            f"{' '.join(argv)}: timed out after {timeout:g}s\n{tail(out or '')}"
        ) from exc
    return Completed(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_s=time.monotonic() - started,
    )
