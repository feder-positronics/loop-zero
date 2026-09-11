#!/usr/bin/env python3
"""Run a base-governed hook and authenticate its terminal result host-side."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from loopzero.config import ConfigError, PRIVILEGED_HOOKS, effective_hooks, load_profile, resolve_base
from loopzero.trust import allowed_path, git_environment, resolve_executable

from .authority import (
    PROOF_FIELD,
    TerminalAuthorityError,
    TerminalAuthorityOperationalError,
    verify_terminal_authority,
)
from .git_config_security import validated_git_config_entries
from .sandbox import SandboxError, run_validation_child
from .settings import settings

RESULT_SCHEMA = "loopzero-validation-result-v2"
MAX_RESULT_BYTES = 1024 * 1024
MAX_PUBLIC_KEY_BYTES = 2048
RESULT_READ_TIMEOUT_S = 2.0
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_MAX_GIT_CONTROL_BYTES = 16 * 1024 * 1024


class ValidationHookError(RuntimeError):
    """The requested hook is absent, ambiguous, or not safely executable."""


class UnsignedResultError(ValidationHookError):
    """The hook did not produce a coordinator-signed terminal result."""


class TamperedResultError(ValidationHookError):
    """The artifact has an invalid coordinator signature."""


class UnboundResultError(ValidationHookError):
    """The signed artifact belongs to a different invocation."""


class _ReadDeadlineExpired(RuntimeError):
    """A signal interrupted a potentially blocking result-file operation."""


@dataclass(frozen=True)
class SourceIdentity:
    """The exact commit or candidate tree presented to validation."""

    kind: str
    sha: str
    filesystem_sha256: str


@dataclass(frozen=True)
class _RepositoryLayout:
    git_dir: Path
    common_dir: Path
    object_dir: Path


@dataclass(frozen=True)
class _GitConfigOverlay:
    destination: Path
    payload: bytes


@contextmanager
def _hard_monotonic_deadline(deadline: float):
    """Interrupt every syscall in a read transaction at one monotonic deadline."""
    if not hasattr(signal, "setitimer"):
        raise _ReadDeadlineExpired
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ReadDeadlineExpired
    try:
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()

        def expired(_signum, _frame):
            raise _ReadDeadlineExpired

        signal.signal(signal.SIGALRM, expired)
    except (AttributeError, ValueError) as exc:
        # Signal timers are process-wide and only enforceable from the main
        # thread. A caller for which the bound cannot be installed must fail.
        raise _ReadDeadlineExpired from exc
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(previous_timer[0] - elapsed, 1e-6),
                previous_timer[1],
            )


def _command_argv(root: Path, command: str, extra: list[str]) -> list[str]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValidationHookError(f"approved hook command is invalid: {exc}") from exc
    if not argv:
        raise ValidationHookError("approved hook command is empty")
    executable = resolve_executable(root, argv[0], allowed_path(()))
    if executable is None:
        raise ValidationHookError(
            f"approved hook executable {argv[0]!r} is outside the shared allowlist"
        )
    # Only argv[0] is host-selected. Remaining operands intentionally name
    # candidate inputs and tests; their output is authenticated separately.
    return [str(executable), *argv[1:], *extra]


def _read_regular(
    path: Path,
    *,
    maximum: int,
    label: str,
    error_type: type[ValidationHookError] = ValidationHookError,
    timeout: float = RESULT_READ_TIMEOUT_S,
) -> bytes:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    try:
        with _hard_monotonic_deadline(deadline):
            descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > maximum
            ):
                raise error_type(f"{label} must be a bounded regular file")
            payload = bytearray()
            while len(payload) < metadata.st_size:
                if time.monotonic() >= deadline:
                    raise _ReadDeadlineExpired
                try:
                    chunk = os.read(
                        descriptor,
                        min(64 * 1024, metadata.st_size - len(payload)),
                    )
                except BlockingIOError:
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                    continue
                if not chunk:
                    break
                payload.extend(chunk)
            current = os.fstat(descriptor)
            if time.monotonic() >= deadline:
                raise _ReadDeadlineExpired
            identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if len(payload) != metadata.st_size or any(
                getattr(current, field) != getattr(metadata, field) for field in identity
            ):
                raise error_type(f"{label} changed while being read")
            return bytes(payload)
    except _ReadDeadlineExpired as exc:
        raise error_type(f"{label} read timed out") from exc
    except OSError as exc:
        raise error_type(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _git(
    root: Path,
    git_dir: Path,
    object_dir: Path,
    *arguments: str,
    index: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = git_environment({"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"})
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(root),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(object_dir),
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        [
            "/usr/bin/git",
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.worktree={root}",
            "-c",
            "core.bare=false",
            "-c",
            "core.symlinks=true",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-c",
            "core.alternateRefsCommand=",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _git_output(
    root: Path,
    git_dir: Path,
    object_dir: Path,
    *arguments: str,
    index: Path | None = None,
    label: str,
) -> str:
    completed = _git(root, git_dir, object_dir, *arguments, index=index)
    if completed.returncode != 0:
        raise ValidationHookError(f"validation worktree {label} is unavailable")
    return completed.stdout.strip()


def _no_follow_state(path: Path, *, label: str) -> tuple[Path, os.stat_result]:
    """Walk one lexical path without allowing any component to be a symlink."""
    lexical = path if path.is_absolute() else Path.cwd() / path
    current = Path(lexical.anchor)
    try:
        state = os.lstat(current)
        parts = lexical.parts[1:]
        for index, part in enumerate(parts):
            if part == "..":
                current = current.parent
                state = os.lstat(current)
                continue
            if part == ".":
                continue
            current /= part
            state = os.lstat(current)
            if stat.S_ISLNK(state.st_mode):
                raise ValidationHookError(
                    f"validation worktree {label} contains a symlinked component"
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(state.st_mode):
                raise ValidationHookError(
                    f"validation worktree {label} is unavailable"
                )
        return Path(os.path.normpath(current)), state
    except ValidationHookError:
        raise
    except OSError as exc:
        raise ValidationHookError(f"validation worktree {label} is unavailable") from exc


def _resolved_git_directory(path: Path, *, label: str) -> Path:
    lexical, state = _no_follow_state(path, label=label)
    if not stat.S_ISDIR(state.st_mode):
        raise ValidationHookError(f"validation worktree {label} is unavailable")
    return lexical


def _repository_layout(root: Path) -> _RepositoryLayout:
    """Locate Git metadata without asking candidate-configured Git to do so."""
    root, root_state = _no_follow_state(root, label="root")
    if not stat.S_ISDIR(root_state.st_mode):
        raise ValidationHookError("validation worktree root is unavailable")
    entry, entry_state = _no_follow_state(root / ".git", label="Git metadata")
    if stat.S_ISDIR(entry_state.st_mode):
        git_dir = _resolved_git_directory(entry, label="Git directory")
    elif stat.S_ISREG(entry_state.st_mode):
        raw = _read_regular(
            entry,
            maximum=4096,
            label="validation worktree Git file",
        )
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValidationHookError("validation worktree Git file is invalid") from exc
        if not line.startswith("gitdir: ") or "\n" in line:
            raise ValidationHookError("validation worktree Git file is invalid")
        selected = Path(line[8:])
        git_dir = _resolved_git_directory(
            selected if selected.is_absolute() else root / selected,
            label="Git directory",
        )
    else:
        raise ValidationHookError("validation worktree Git metadata is unsafe")

    common_file = git_dir / "commondir"
    if common_file.exists() or common_file.is_symlink():
        raw_common = _read_regular(
            common_file,
            maximum=4096,
            label="validation worktree common Git directory",
        )
        try:
            selected_common = Path(raw_common.decode("utf-8").strip())
        except UnicodeDecodeError as exc:
            raise ValidationHookError(
                "validation worktree common Git directory is invalid"
            ) from exc
        common_dir = _resolved_git_directory(
            selected_common if selected_common.is_absolute() else git_dir / selected_common,
            label="common Git directory",
        )
    else:
        common_dir = git_dir
    object_dir = _resolved_git_directory(common_dir / "objects", label="object directory")
    return _RepositoryLayout(git_dir, common_dir, object_dir)


def _config_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\b", "\\b")
    )
    return f'"{escaped}"'


def _sanitized_config(entries: tuple[tuple[str, str | None], ...]) -> bytes:
    """Retain only inert repository identity and tracking configuration."""
    lines = ["[core]", "\trepositoryformatversion = 0", "\tbare = false"]
    for key, value in entries:
        if value is None:
            continue
        normalized = key.casefold()
        pieces = key.split(".")
        if (
            len(pieces) >= 3
            and normalized.startswith("remote.")
            and pieces[-1].casefold() in {"url", "fetch"}
        ):
            subsection = ".".join(pieces[1:-1])
            lines.extend(
                (
                    f'[remote {_config_quote(subsection)}]',
                    f"\t{pieces[-1]} = {_config_quote(value)}",
                )
            )
        elif (
            len(pieces) >= 3
            and normalized.startswith("branch.")
            and pieces[-1].casefold() in {"merge", "remote"}
        ):
            subsection = ".".join(pieces[1:-1])
            lines.extend(
                (
                    f'[branch {_config_quote(subsection)}]',
                    f"\t{pieces[-1]} = {_config_quote(value)}",
                )
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _repository_config_overlays(
    layout: _RepositoryLayout,
) -> tuple[_GitConfigOverlay, ...]:
    overlays: list[_GitConfigOverlay] = []
    for config in dict.fromkeys(
        (layout.git_dir / "config", layout.common_dir / "config")
    ):
        try:
            config_state = config.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValidationHookError(
                "validation worktree Git configuration is unavailable"
            ) from exc
        if not stat.S_ISREG(config_state.st_mode):
            raise ValidationHookError(
                "validation worktree Git configuration is unsafe"
            )
        try:
            raw = _read_regular(
                config,
                maximum=_MAX_GIT_CONTROL_BYTES,
                label="validation worktree Git configuration",
            )
        except ValidationHookError as exc:
            raise ValidationHookError(
                "validation worktree Git configuration is unavailable"
            ) from exc
        try:
            entries = validated_git_config_entries(raw)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise ValidationHookError(
                "validation worktree Git configuration is unsafe"
            ) from exc
        overlays.append(_GitConfigOverlay(config, _sanitized_config(entries)))
    return tuple(overlays)


def _source_filesystem_sha256(root: Path) -> str:
    """Digest every exposed node that Git's index cannot fully represent."""
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def walk(path: Path, relative: Path) -> None:
        try:
            state = path.lstat()
        except OSError as exc:
            raise ValidationHookError(
                "validation worktree filesystem identity is unavailable"
            ) from exc
        mode = stat.S_IMODE(state.st_mode)
        if stat.S_ISREG(state.st_mode):
            kind = b"file"
        elif stat.S_ISDIR(state.st_mode):
            kind = b"directory"
        elif stat.S_ISLNK(state.st_mode):
            kind = b"symlink"
        else:
            raise ValidationHookError(
                "validation worktree contains an unsupported special node"
            )
        add(os.fsencode(str(relative)))
        add(kind)
        add(f"{mode:o}".encode("ascii"))
        if kind == b"symlink":
            try:
                add(os.fsencode(os.readlink(path)))
            except OSError as exc:
                raise ValidationHookError(
                    "validation worktree filesystem identity is unavailable"
                ) from exc
            return
        if kind != b"directory":
            return
        try:
            children = sorted(
                path.iterdir(), key=lambda child: os.fsencode(child.name)
            )
        except OSError as exc:
            raise ValidationHookError(
                "validation worktree filesystem identity is unavailable"
            ) from exc
        for child in children:
            child_relative = relative / child.name if relative != Path(".") else Path(child.name)
            if child_relative == Path(".git"):
                continue
            walk(child, child_relative)

    walk(root, Path("."))
    return digest.hexdigest()


def _valid_ref_name(name: str) -> bool:
    if not name.startswith("refs/") or name.endswith(("/", ".")):
        return False
    if any(character.isspace() or ord(character) < 0x20 for character in name):
        return False
    if any(token in name for token in ("..", "//", "@{", "\\", "~", "^", ":", "?", "*", "[")):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


def _head_sha(layout: _RepositoryLayout) -> str:
    """Resolve HEAD from bounded no-follow control files, including packed refs."""
    raw_head = _read_regular(
        layout.git_dir / "HEAD", maximum=4096, label="validation worktree HEAD"
    )
    try:
        value = raw_head.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValidationHookError("validation worktree HEAD is invalid") from exc
    if _SHA_RE.fullmatch(value):
        return value
    if not value.startswith("ref: ") or not _valid_ref_name(value[5:]):
        raise ValidationHookError("validation worktree HEAD is invalid")
    ref_name = value[5:]
    for base in dict.fromkeys((layout.git_dir, layout.common_dir)):
        ref_path = base / ref_name
        if ref_path.exists() or ref_path.is_symlink():
            raw_ref = _read_regular(
                ref_path, maximum=4096, label="validation worktree HEAD reference"
            )
            try:
                sha = raw_ref.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise ValidationHookError("validation worktree HEAD is invalid") from exc
            if not _SHA_RE.fullmatch(sha):
                raise ValidationHookError("validation worktree HEAD is invalid")
            return sha
    packed = layout.common_dir / "packed-refs"
    if packed.exists() or packed.is_symlink():
        raw_packed = _read_regular(
            packed,
            maximum=_MAX_GIT_CONTROL_BYTES,
            label="validation worktree packed references",
        )
        for line in raw_packed.splitlines():
            sha, separator, raw_name = line.partition(b" ")
            if separator and raw_name == ref_name.encode("utf-8"):
                try:
                    decoded_sha = sha.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ValidationHookError("validation worktree HEAD is invalid") from exc
                if _SHA_RE.fullmatch(decoded_sha):
                    return decoded_sha
                break
    raise ValidationHookError("validation worktree HEAD is unavailable")


@contextmanager
def _isolated_git_repository(root: Path, layout: _RepositoryLayout):
    """Build a config-free Git view whose attributes disable all conversions."""
    with tempfile.TemporaryDirectory(prefix="loopzero-validation-git-") as raw:
        git_dir = Path(raw) / "git"
        (git_dir / "objects").mkdir(parents=True)
        (git_dir / "refs").mkdir()
        (git_dir / "info").mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/isolated\n", encoding="ascii")
        (git_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
            encoding="ascii",
        )
        # info/attributes has higher precedence than candidate .gitattributes.
        # Disabling every check-in conversion makes the tree a hash of raw file
        # bytes and Git modes. Candidate attribute files remain hashed inputs,
        # but cannot select filters, encodings, ident expansion, or EOL changes.
        (git_dir / "info" / "attributes").write_text(
            "** -text -eol -filter -ident -working-tree-encoding\n",
            encoding="ascii",
        )
        yield git_dir, Path(raw) / "index"


def _dirty_tree_sha(
    root: Path, *, approved_head: str, layout: _RepositoryLayout
) -> tuple[str, str]:
    """Hash every exposed worktree entry through a config-free isolated index.

    ``--force`` includes ignored files. The only omitted path is the root Git
    administrative entry, which the validation sandbox separately seals
    read-only. Gitlink entries are rejected because their checked-out contents
    would otherwise remain executable without entering the outer tree hash.
    """
    with _isolated_git_repository(root, layout) as (git_dir, index):
        commit = _git_output(
            root,
            git_dir,
            layout.object_dir,
            "rev-parse",
            "--verify",
            f"{approved_head}^{{commit}}",
            label="HEAD commit",
        )
        head_tree = _git_output(
            root,
            git_dir,
            layout.object_dir,
            "rev-parse",
            "--verify",
            f"{approved_head}^{{tree}}",
            label="HEAD tree",
        )
        _git_output(
            root,
            git_dir,
            layout.object_dir,
            "read-tree",
            approved_head,
            index=index,
            label="tree",
        )
        _git_output(
            root,
            git_dir,
            layout.object_dir,
            "add",
            "--all",
            "--force",
            "--no-renormalize",
            "--",
            ".",
            ":(exclude,top).git",
            index=index,
            label="tree",
        )
        staged = _git_output(
            root,
            git_dir,
            layout.object_dir,
            "ls-files",
            "--stage",
            index=index,
            label="tree",
        )
        if any(row.startswith("160000 ") for row in staged.splitlines()):
            raise ValidationHookError(
                "validation worktree contains an unbound nested Git worktree"
            )
        tree = _git_output(
            root,
            git_dir,
            layout.object_dir,
            "write-tree",
            index=index,
            label="tree",
        )
    if commit != approved_head or not _SHA_RE.fullmatch(tree) or not _SHA_RE.fullmatch(head_tree):
        raise ValidationHookError("validation worktree tree identity is invalid")
    return tree, head_tree


def _prepared_source_identity(
    root: Path, *, approved_head: str, allow_dirty_tree: bool
) -> tuple[SourceIdentity, tuple[_GitConfigOverlay, ...]]:
    """Prove HEAD and bind either its clean commit or an intentional dirty tree."""
    layout = _repository_layout(root)
    overlays = _repository_config_overlays(layout)
    head = _head_sha(layout)
    if head != approved_head:
        raise ValidationHookError(
            "validation worktree HEAD does not match the approved head"
        )
    filesystem_sha256 = _source_filesystem_sha256(root)
    tree, head_tree = _dirty_tree_sha(
        root, approved_head=approved_head, layout=layout
    )
    if tree == head_tree:
        return SourceIdentity("commit", approved_head, filesystem_sha256), overlays
    if not allow_dirty_tree:
        raise ValidationHookError(
            "validation worktree is dirty; --allow-dirty-tree is required"
        )
    return SourceIdentity("tree", tree, filesystem_sha256), overlays


def source_identity(
    root: Path, *, approved_head: str, allow_dirty_tree: bool
) -> SourceIdentity:
    return _prepared_source_identity(
        root, approved_head=approved_head, allow_dirty_tree=allow_dirty_tree
    )[0]


def read_coordinator_public_key(path: Path) -> bytes:
    """Snapshot caller-selected authority before candidate code executes."""
    return _read_regular(path, maximum=MAX_PUBLIC_KEY_BYTES, label="coordinator public key")


def verify_result_artifact(
    path: Path,
    *,
    coordinator_public_key: bytes,
    task_id: str,
    base_sha: str,
    head_sha: str,
    hook: str,
    hook_commands: list[str],
    source: SourceIdentity,
) -> dict[str, object]:
    try:
        artifact = json.loads(
            _read_regular(
                path,
                maximum=MAX_RESULT_BYTES,
                label="validation result artifact",
                error_type=UnsignedResultError,
            )
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UnsignedResultError("validation result artifact is not canonical JSON") from exc
    if not isinstance(artifact, dict) or not isinstance(artifact.get(PROOF_FIELD), dict):
        raise UnsignedResultError("validation result artifact is unsigned")
    try:
        verify_terminal_authority(
            artifact,
            registration=None,
            expected_kind="coordinator",
            coordinator_public_key=coordinator_public_key,
        )
    except TerminalAuthorityOperationalError as exc:
        raise ValidationHookError(
            f"validation result verifier is unavailable: {exc}"
        ) from exc
    except TerminalAuthorityError as exc:
        raise TamperedResultError(f"validation result signature is invalid: {exc}") from exc
    expected = {
        "schema_version": RESULT_SCHEMA,
        "task_id": task_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "source_kind": source.kind,
        "source_sha": source.sha,
        "source_filesystem_sha256": source.filesystem_sha256,
        "hook": hook,
        "hook_commands": hook_commands,
        "status": "completed",
        "exit_code": 0,
    }
    mismatches = [name for name, value in expected.items() if artifact.get(name) != value]
    if set(artifact) != {*expected, PROOF_FIELD}:
        mismatches.append("fields")
    if mismatches:
        raise UnboundResultError(
            "validation result is not bound to this invocation: "
            + ", ".join(dict.fromkeys(mismatches))
        )
    return artifact


def run_hook(
    *,
    worktree: Path,
    hook: str,
    base_sha: str,
    head_sha: str,
    task_id: str,
    result_artifact: Path,
    coordinator_public_key: bytes,
    extra: list[str],
    allow_dirty_tree: bool = False,
) -> int:
    root = worktree.absolute()
    if hook not in PRIVILEGED_HOOKS:
        raise ValidationHookError(f"{hook!r} is not a privileged validation hook")
    # Reject unsafe metadata and snapshot the only repository configuration the
    # child may see before any candidate-governed validation process starts.
    _repository_config_overlays(_repository_layout(root))
    approved_base = resolve_base(root, base=base_sha)
    approved_head = resolve_base(root, base=head_sha)
    source, config_overlays = _prepared_source_identity(
        root, approved_head=approved_head, allow_dirty_tree=allow_dirty_tree
    )
    profile = load_profile(root)
    commands = effective_hooks(profile, approved_base).get(hook)
    if not commands:
        raise ValidationHookError(f"approved {hook!r} hook is unavailable at {approved_base}")
    if extra and len(commands) != 1:
        raise ValidationHookError("hook arguments require exactly one approved command")
    timeout = settings.toolchain.get("validation_timeout_s", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValidationHookError("validation timeout setting is invalid")
    actual_commands: list[str] = []
    for command in commands:
        argv = _command_argv(root, command, extra)
        actual_commands.append(shlex.join(argv))
        result = run_validation_child(
            argv,
            worktree=root,
            timeout=float(timeout),
            git_config_overlays=tuple(
                (overlay.destination, overlay.payload) for overlay in config_overlays
            ),
        )
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode != 0:
            return result.returncode
    try:
        after = source_identity(
            root, approved_head=approved_head, allow_dirty_tree=allow_dirty_tree
        )
    except ValidationHookError as exc:
        raise UnboundResultError(
            "validation worktree source changed during hook execution"
        ) from exc
    if after != source:
        raise UnboundResultError(
            "validation worktree source changed during hook execution"
        )
    verify_result_artifact(
        result_artifact,
        coordinator_public_key=coordinator_public_key,
        task_id=task_id,
        base_sha=approved_base,
        head_sha=approved_head,
        hook=hook,
        hook_commands=actual_commands,
        source=source,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--result-artifact", type=Path, required=True)
    parser.add_argument("--coordinator-public-key", type=Path, required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("--allow-dirty-tree", action="store_true")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    try:
        public_key = read_coordinator_public_key(args.coordinator_public_key)
        return run_hook(
            worktree=args.worktree,
            hook=args.hook,
            base_sha=args.base,
            head_sha=args.head,
            task_id=args.task_id,
            result_artifact=args.result_artifact,
            coordinator_public_key=public_key,
            extra=extra,
            allow_dirty_tree=args.allow_dirty_tree,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"validation hook timed out after {exc.timeout}s", file=sys.stderr)
        return 124
    except UnsignedResultError as exc:
        print(f"validation result rejected (unsigned): {exc}", file=sys.stderr)
        return 125
    except TamperedResultError as exc:
        print(f"validation result rejected (signature): {exc}", file=sys.stderr)
        return 126
    except UnboundResultError as exc:
        print(f"validation result rejected (binding): {exc}", file=sys.stderr)
        return 127
    except (ConfigError, SandboxError, ValidationHookError) as exc:
        print(f"validation hook unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
