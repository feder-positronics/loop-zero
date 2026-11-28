"""The single subprocess helper. Every external command goes through `run`."""

from __future__ import annotations

import codecs
import io
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

from .types import LoopZeroError

TAIL_LINES = 40


class ToolMissing(LoopZeroError):
    """The executable named by argv[0] could not be found on PATH."""


class ProcTimeout(LoopZeroError):
    """Supervisor timeout with captured streams and elapsed time."""

    def __init__(
        self, message: str, stdout: str = "", *, stderr: str = "", duration_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.duration_s = duration_s


class Terminated(BaseException):
    """Unwind the active operation before exiting from SIGTERM."""


def _request_unwind(_signum, _frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise Terminated


@contextmanager
def unwind_on_sigterm():
    if (threading.current_thread() is not threading.main_thread()
            or signal.getsignal(signal.SIGTERM) != signal.SIG_DFL):
        yield
        return
    try:
        try:
            signal.signal(signal.SIGTERM, _request_unwind)
            yield
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except Terminated:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.raise_signal(signal.SIGTERM)
        raise


@contextmanager
def defer_sigterm():
    """Finish a private directory operation before unwinding."""
    if (threading.current_thread() is not threading.main_thread()
            or signal.getsignal(signal.SIGTERM) is not _request_unwind):
        yield
        return
    pending = False

    def remember_signal(_signum, _frame):
        nonlocal pending
        pending = True

    signal.signal(signal.SIGTERM, remember_signal)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, _request_unwind)
        if pending:
            _request_unwind(signal.SIGTERM, None)


@dataclass(frozen=True)
class Completed:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


_KEY_LEADER = "-----BEGIN "
_KEY_START = rf"{_KEY_LEADER}[A-Z ]*PRIVATE KEY-----"
_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_KEY_BOUNDARY = re.compile(rf"({_KEY_START})|({_KEY_END.pattern})|{_KEY_LEADER}[A-Z ]*+-{{0,4}}\Z")
SECRET_PATTERNS = (
    r"\b(?:github_pat_[A-Za-z0-9_]+\b|gh[pousr]_[A-Za-z0-9_]{10,}\b|sk-[A-Za-z0-9_-]{8,})",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", r"\brt_[A-Za-z0-9_.-]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
    rf"{_KEY_START}[\s\S]*?(?:{_KEY_END.pattern}|\Z)",
    r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)",  # URL userinfo with a password
)


def secret_forms(values: Iterable[str]) -> list[str]:
    """Stripped values with their JSON-escaped and URL-quoted forms, longest first."""
    stripped = filter(None, (value.strip() for value in values))
    return sorted({form for value in stripped for form in (
        value, value.replace("\r\n", "\n").replace("\r", "\n"), json.dumps(value)[1:-1], quote(value, safe=""))},
                  key=len, reverse=True)


def redact(text: str, forms: Sequence[str], *patterns: str, skip: int = 0) -> str:
    spans = [(m.start(), m.start() + len(form)) for form in forms for m in re.finditer(rf"(?={re.escape(form)})", text)]
    spans.extend(m.span() for pattern in patterns for m in re.finditer(pattern, text))
    parts = []
    for start, end in sorted(spans):
        if start >= skip:
            parts.extend((text[skip:start], "[REDACTED]"))
        skip = max(skip, end)
    return "".join([*parts, text[skip:]])


def build_env(env_allowlist: Sequence[str], extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    return {**{key: os.environ[key] for key in env_allowlist if key in os.environ}, **(extra_env or {})}


def _redact_capture(text: str, forms: Sequence[str], lead: int, clipped: bool, key_open: bool | None) -> str:
    # The first record can start inside a token or a forwarded multiline value.
    skip = (text.find("\n", lead) + 1 or len(text)) if clipped else 0
    if key_open:
        end = _KEY_END.search(text)
        skip = max(skip, end.end() if end else len(text))
    return redact(text, forms, *SECRET_PATTERNS, skip=skip)


def _read_output_tail(proc: subprocess.Popen[str], limit: int, timeout: float, forms: Sequence[str] | None = None) -> str:
    stream = proc.stdout
    assert stream is not None
    decode = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder(stream.encoding)(errors="replace"), translate=True,
    ).decode
    deadline = time.monotonic() + timeout
    suffix = ""
    newlines = 0
    lead = max(map(len, forms or ()), default=0)
    window = limit if forms is None else limit + 64 * 1024 + lead
    clipped = key_open = False  # A key state of None means the opener is incomplete.

    def append(chunk: str) -> None:
        nonlocal suffix, newlines, clipped, key_open
        content = chunk.rstrip("\n")
        if content:
            text = suffix + "\n" * newlines + content
            cut = max(0, len(text) - window)
            if forms is not None and cut:
                clipped = True
                leader = _KEY_LEADER if key_open is None else ""
                key_open = bool(key_open)
                for marker in (m for m in _KEY_BOUNDARY.finditer(leader + text) if m.start() < cut + len(leader)):
                    key_open = True if marker[1] else False if marker[2] else key_open or None
                    cut = max(cut, marker.start() + len(_KEY_LEADER) - len(leader)) if key_open is None else cut
            suffix = text[cut:]
            newlines = 0
        newlines = min(window, newlines + len(chunk) - len(content))

    def finish() -> str:
        append(decode(b"", final=True))
        return suffix if forms is None else _redact_capture(suffix, forms, lead, clipped, key_open)[-limit:]

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(proc.args, timeout)
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    break
                append(decode(chunk))
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        exc.output = (finish() + "\n" * min(limit, newlines))[-limit:]
        raise
    return finish().rstrip("\n")


def run_git_paths(argv: Sequence[str], *, cwd: Path, env_allowlist: Sequence[str], timeout: float,
                  extra_env: Mapping[str, str] | None = None) -> Completed:
    done = run(argv, cwd=cwd, env_allowlist=env_allowlist, timeout=timeout, extra_env=extra_env,
               preserve_newlines=True, encoding=sys.getfilesystemencoding())
    return replace(done, stdout=done.stdout.removesuffix("\n"))


def run(
    argv: Sequence[str], *, cwd: Path | str, env_allowlist: Sequence[str],
    extra_env: Mapping[str, str] | None = None, timeout: float,
    merge_output: bool = False, input: str | None = None, encoding: str | None = None,
    preserve_newlines: bool = False, tail_chars: int | None = None, graceful_interrupt: bool = False,
    pass_fds: tuple[int, ...] = (), tail_secrets: Sequence[str] | None = None,
) -> Completed:
    """Run directly with a filtered environment and capture the exit code.

    Preserve raw newlines and undecodable stdout with `preserve_newlines`. A
    bounded merged tail retains trailing newlines only on timeout.
    """
    if tail_chars is not None and (not merge_output or preserve_newlines or input is not None or tail_chars <= 0):
        raise ValueError("bounded tails require merged text output, no input and a positive length")
    argv = [str(part) for part in argv]
    started = time.monotonic()
    raw_encoding = encoding or "utf-8"
    stdin = input.encode(raw_encoding) if preserve_newlines and input is not None else input
    try:
        with subprocess.Popen(
            argv, cwd=str(cwd), env=build_env(env_allowlist, extra_env),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
            encoding=None if preserve_newlines else encoding, errors=None if preserve_newlines else "replace",
            pass_fds=pass_fds, start_new_session=os.name == "posix",
        ) as proc:
            try:
                stdout, stderr = (proc.communicate(stdin, timeout=timeout) if tail_chars is None
                                  else (_read_output_tail(proc, tail_chars, timeout, tail_secrets), ""))
            except BaseException:
                try:
                    if graceful_interrupt and os.name == "posix":
                        with suppress(ProcessLookupError, subprocess.TimeoutExpired):
                            os.killpg(proc.pid, signal.SIGTERM)
                            proc.wait(timeout=5)
                finally:
                    if os.name == "posix":
                        with suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    # Popen's context exit can skip reaping after KeyboardInterrupt.
                    proc.wait()
                raise
    except FileNotFoundError as exc:
        if exc.filename == str(cwd):
            raise FileNotFoundError(f"working directory {cwd} does not exist") from exc
        raise ToolMissing(f"{argv[0]}: executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        elapsed = time.monotonic() - started
        message = f"{' '.join(argv)}: timed out after {timeout:g}s\n{tail(out)}"
        raise ProcTimeout(message, out, stderr=err or "", duration_s=elapsed) from exc
    if isinstance(stdout, bytes):
        stdout = stdout.decode(raw_encoding, errors="surrogateescape")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(raw_encoding, errors="replace")
    return Completed(proc.returncode, stdout or "", stderr or "", time.monotonic() - started)
