#!/usr/bin/python3 -I
"""Launch privileged closeout from a pinned, reviewed-main toolchain snapshot.

An IntelFlo consumer keeps a tiny ``scripts/util/pr_closeout.py`` launcher. It
loads its :class:`~loopzero.config.Profile` and calls ``main(profile=profile,
launcher_path=Path(__file__))``. The configured ``closeout_launcher_path`` and
``closeout_trust_floor_path`` preserve the original locations by default while
allowing another consumer to bind its own canonical-primary adapter paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

TRUSTED_BASH = "/bin/bash"
TRUSTED_GH = "/usr/bin/gh"
TRUSTED_GIT = "/usr/bin/git"
TRUSTED_PYTHON = "/usr/bin/python3"
TRUSTED_PATH = "/usr/bin:/bin"
TRUST_FLOOR_EPOCH = 1
TRUST_FLOOR_FILE = "scripts/util/pr_closeout_trust_floor.json"
CANONICAL_ORIGIN_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
)
OID_RE = re.compile(r"[0-9a-f]{40}")
DEFAULT_BRANCH_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")
ALLOWED_SOURCE_ENVIRONMENT = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "TERM", "COLORTERM"}
)


class BootstrapError(RuntimeError):
    """A fail-closed trusted-bootstrap validation error."""


@dataclass(frozen=True)
class LauncherOptions:
    revision: str | None = None
    rollback_reason: str | None = None
    repair_primary: bool = False


@dataclass(frozen=True)
class DeliveryBinding:
    delivery_root: Path
    git_dir: Path


@dataclass(frozen=True)
class RepositoryBinding:
    origin_url: str
    config_sha256: str


@dataclass(frozen=True)
class CloseoutSettings:
    """Consumer-owned locations around the preserved trusted bootstrap."""

    snapshot_path: Path
    temp_prefix: str
    trusted_bin_dir: Path
    env_prefix: str
    launcher_path: Path
    trust_floor_path: PurePosixPath

    @classmethod
    def from_profile(cls, profile: Any) -> "CloseoutSettings":
        required = ("closeout_snapshot_path", "closeout_temp_prefix", "trusted_bin_dir")
        missing = [name for name in required if not profile.toolchain.get(name)]
        if missing:
            raise BootstrapError("closeout configuration is missing: " + ", ".join(missing))
        return cls(
            Path(profile.toolchain["closeout_snapshot_path"]),
            str(profile.toolchain["closeout_temp_prefix"]),
            Path(profile.toolchain["trusted_bin_dir"]),
            str(profile.env_prefix),
            Path(
                profile.toolchain.get(
                    "closeout_launcher_path", "scripts/util/pr_closeout.py"
                )
            ),
            PurePosixPath(
                profile.toolchain.get(
                    "closeout_trust_floor_path", TRUST_FLOOR_FILE
                )
            ),
        )

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"


def isolated_environment(
    source: dict[str, str] | Any,
    profile_source: dict[str, str] | None = None,
    *,
    home: Path | None = None,
    user: str | None = None,
) -> dict[str, str]:
    """Build the complete child environment from a narrow positive allowlist."""

    configured_path = TRUSTED_PATH
    if profile_source is not None:
        configured_path = str(CloseoutSettings.from_profile(source).trusted_bin_dir)
        source = profile_source
    account = pwd.getpwuid(os.getuid()) if home is None or user is None else None
    trusted_home = Path(account.pw_dir) if home is None and account else Path(home)
    trusted_user = account.pw_name if user is None and account else str(user)
    environment = {
        name: source[name] for name in ALLOWED_SOURCE_ENVIRONMENT if source.get(name)
    }
    environment.update(
        {
            "HOME": str(trusted_home),
            "USER": trusted_user,
            "LOGNAME": trusted_user,
            "GH_CONFIG_DIR": str(trusted_home / ".config/gh"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": configured_path,
            "TMPDIR": "/tmp",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def closeout_environment(
    base: dict[str, str],
    *,
    primary: Path,
    delivery: Path,
    revision: str,
    remote_default_ref: str,
    tree_oid: str,
) -> dict[str, str]:
    """Add trusted closeout metadata without overriding normal job authority."""

    environment = dict(base)
    environment.update(
        {
            "INTELFLO_BOOTSTRAP": "reviewed-main-v1",
            "INTELFLO_TRUSTED_PRIMARY": str(primary),
            "INTELFLO_DELIVERY_ROOT": str(delivery),
            "INTELFLO_TRUSTED_REVISION": revision,
            "INTELFLO_REMOTE_DEFAULT_REF": remote_default_ref,
            "INTELFLO_CLOSEOUT_TOOLCHAIN_OID": tree_oid,
            "INTELFLO_BASH_BIN": TRUSTED_BASH,
            "INTELFLO_GH_BIN": TRUSTED_GH,
            "INTELFLO_GIT_BIN": TRUSTED_GIT,
            "INTELFLO_PYTHON_BIN": TRUSTED_PYTHON,
        }
    )
    return environment


def parse_launcher_arguments(argv: list[str]) -> tuple[LauncherOptions, list[str]]:
    """Consume bootstrap-only options and preserve closeout implementation args."""

    revision: str | None = None
    rollback_reason: str | None = None
    repair_primary = False
    forwarded: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in {"--origin-url", "--git-config-sha256"}:
            raise BootstrapError(f"{argument} is managed by the trusted launcher")
        if argument == "--repair-primary":
            if repair_primary:
                raise BootstrapError("--repair-primary may be supplied only once")
            repair_primary = True
            index += 1
            continue
        if argument not in {"--trusted-revision", "--rollback-reason"}:
            forwarded.append(argument)
            index += 1
            continue
        if index + 1 >= len(argv):
            raise BootstrapError(f"{argument} requires a value")
        value = argv[index + 1]
        if argument == "--trusted-revision":
            if revision is not None:
                raise BootstrapError("--trusted-revision may be supplied only once")
            if OID_RE.fullmatch(value) is None:
                raise BootstrapError("--trusted-revision must be 40 lowercase hex")
            revision = value
        else:
            if rollback_reason is not None:
                raise BootstrapError("--rollback-reason may be supplied only once")
            rollback_reason = value.strip()
        index += 2
    if rollback_reason is not None and revision is None:
        raise BootstrapError("--rollback-reason requires --trusted-revision")
    if repair_primary and (revision is not None or forwarded):
        raise BootstrapError(
            "--repair-primary cannot be combined with closeout or rollback arguments"
        )
    return LauncherOptions(revision, rollback_reason, repair_primary), forwarded


def _run_git(
    repo: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    text: bool = True,
    input_data: bytes | str | None = None,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            [TRUSTED_GIT, "-C", str(repo), *arguments],
            input=input_data,
            capture_output=True,
            text=text,
            env=environment or isolated_environment({}),
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            f"trusted Git command timed out after {timeout} seconds"
        ) from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise BootstrapError(f"trusted Git command failed: {detail}")
    return completed


def _canonical_primary(
    launcher: Path,
    expected_launcher_path: Path = Path("scripts/util/pr_closeout.py"),
) -> Path:
    expected_relative = PurePosixPath(expected_launcher_path.as_posix())
    if (
        expected_relative.is_absolute()
        or not expected_relative.parts
        or ".." in expected_relative.parts
    ):
        raise BootstrapError("configured closeout launcher path is unsafe")
    if launcher.is_symlink() or not launcher.is_file():
        raise BootstrapError("trusted launcher must be one regular file")
    resolved = launcher.resolve()
    try:
        primary = resolved.parents[len(expected_relative.parts) - 1]
    except IndexError as exc:
        raise BootstrapError(
            "trusted launcher path is outside scripts/util or its configured root"
        ) from exc
    expected = primary.joinpath(*expected_relative.parts)
    if resolved != expected:
        raise BootstrapError(
            "trusted launcher path is outside scripts/util or its configured root"
        )
    git_dir = primary / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise BootstrapError(
            "trusted launcher must come from the canonical primary checkout"
        )
    return primary


def _worktree_paths(raw: bytes) -> frozenset[Path]:
    paths: set[Path] = set()
    for field in raw.split(b"\0"):
        if not field.startswith(b"worktree "):
            continue
        try:
            paths.add(Path(field.removeprefix(b"worktree ").decode("utf-8")).resolve())
        except UnicodeDecodeError as exc:
            raise BootstrapError("primary worktree metadata is not UTF-8") from exc
    return frozenset(paths)


def _regular_text(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"{label} is not one regular file")
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"{label} cannot be read") from exc


def verify_delivery_binding(primary: Path, delivery: Path) -> DeliveryBinding:
    """Prove delivery metadata is registered by the launcher's primary repo."""

    primary = primary.resolve()
    delivery = delivery.resolve()
    result = _run_git(primary, "worktree", "list", "--porcelain", "-z", text=False)
    if delivery not in _worktree_paths(result.stdout):
        raise BootstrapError("delivery root is not registered by the trusted primary")
    if delivery == primary:
        return DeliveryBinding(delivery, primary / ".git")

    candidate_git = delivery / ".git"
    if candidate_git.is_symlink() or not candidate_git.is_file():
        raise BootstrapError("delivery root has no regular registered Git metadata")
    worktrees_dir = primary / ".git/worktrees"
    if worktrees_dir.is_symlink() or not worktrees_dir.is_dir():
        raise BootstrapError("primary linked-worktree metadata is unavailable")
    for admin_dir in worktrees_dir.iterdir():
        if admin_dir.is_symlink() or not admin_dir.is_dir():
            continue
        registered_git_file = admin_dir / "gitdir"
        try:
            registered_path = Path(
                _regular_text(registered_git_file, "registered Git metadata")
            ).resolve()
        except BootstrapError:
            continue
        if registered_path != candidate_git.resolve():
            continue
        pointer = _regular_text(candidate_git, "delivery Git metadata")
        prefix = "gitdir: "
        if not pointer.startswith(prefix):
            break
        pointed_admin = Path(pointer.removeprefix(prefix)).resolve()
        common_dir = (
            admin_dir / _regular_text(admin_dir / "commondir", "common Git metadata")
        ).resolve()
        if (
            pointed_admin == admin_dir.resolve()
            and common_dir == (primary / ".git").resolve()
        ):
            return DeliveryBinding(delivery, admin_dir.resolve())
        break
    raise BootstrapError("delivery root does not match its registered Git metadata")


def ensure_authority_clean(primary: Path) -> None:
    head = _run_git(primary, "rev-parse", "HEAD", check=False)
    flags = _run_git(primary, "ls-files", "-v", "-z", text=False, check=False)
    index_tree = _run_git(primary, "write-tree", check=False)
    head_tree = _run_git(primary, "rev-parse", "HEAD^{tree}", check=False)
    refreshed = _run_git(
        primary, "update-index", "--really-refresh", check=False
    )
    status = _run_git(
        primary,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        check=False,
    )
    entries = [entry for entry in flags.stdout.split(b"\0") if entry]
    if flags.returncode or not all(entry.startswith(b"H ") for entry in entries):
        raise BootstrapError("canonical primary has nonordinary index flags")
    if (
        head.returncode
        or index_tree.returncode
        or head_tree.returncode
        or refreshed.returncode
        or status.returncode
        or index_tree.stdout.strip() != head_tree.stdout.strip()
        or status.stdout.strip()
    ):
        raise BootstrapError("canonical primary is dirty")


def _git_config_key_can_redirect(name: str) -> bool:
    # This bootstrap copy is intentionally self-contained: importing a sibling
    # before source pinning would widen the first-code boundary. Focused parity
    # tests compare it and the projection digest with git_config_security.py.
    normalized = name.casefold()
    if normalized.startswith(
        (
            "credential.",
            "http.",
            "https.",
            "include.",
            "includeif.",
            "url.",
            "protocol.",
            "diff.",
            "filter.",
            "alias.",
            "gpg.",
        )
    ):
        return True
    if normalized.startswith("merge.") and normalized.endswith(".driver"):
        return True
    if normalized in {
        "extensions.worktreeconfig",
        "core.askpass",
        "core.editor",
        "core.fsmonitor",
        "core.gitproxy",
        "core.hookspath",
        "core.pager",
        "core.alternaterefscommand",
        "core.sshcommand",
        "core.worktree",
    }:
        return True
    return normalized.startswith("remote.origin.") and normalized not in {
        "remote.origin.fetch",
        "remote.origin.gh-resolved",
        "remote.origin.url",
    }


def _parse_config(
    raw_config: bytes, environment: dict[str, str]
) -> tuple[tuple[str, str | None], ...]:
    completed = subprocess.run(
        [TRUSTED_GIT, "config", "--file", "-", "--no-includes", "--null", "--list"],
        input=raw_config,
        capture_output=True,
        check=False,
        env=environment,
        timeout=5,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BootstrapError(f"canonical Git config cannot be parsed: {detail}")
    entries: list[tuple[str, str | None]] = []
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        raw_key, separator, raw_value = record.partition(b"\n")
        try:
            key = raw_key.decode("utf-8")
            value = raw_value.decode("utf-8") if separator else None
        except UnicodeDecodeError as exc:
            raise BootstrapError("canonical Git config is not UTF-8") from exc
        if _git_config_key_can_redirect(key):
            raise BootstrapError(
                "canonical Git config uses unmeasured execution or redirection"
            )
        entries.append((key, value))
    return tuple(entries)


def read_repository_binding(
    primary: Path, environment: dict[str, str]
) -> RepositoryBinding:
    config = primary / ".git/config"
    if config.is_symlink() or not config.is_file():
        raise BootstrapError("canonical Git configuration is unavailable")
    try:
        raw_config = config.read_bytes()
    except OSError as exc:
        raise BootstrapError("canonical Git configuration cannot be read") from exc
    entries = _parse_config(raw_config, environment)
    urls = [
        value
        for key, value in entries
        if key.casefold() == "remote.origin.url" and value is not None
    ]
    if len(urls) != 1 or CANONICAL_ORIGIN_RE.fullmatch(urls[0]) is None:
        raise BootstrapError("canonical Git origin must be one GitHub HTTPS URL")
    projection = [
        (key, value)
        for key, value in entries
        if not key.casefold().startswith("branch.")
    ]
    digest = hashlib.sha256(
        json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return RepositoryBinding(urls[0], digest)


def _validate_system_tool(path: str, label: str) -> None:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise BootstrapError(f"pinned {label} executable is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise BootstrapError(f"pinned {label} executable is unavailable")
    acceptable_owners = {0, os.getuid(), os.stat("/").st_uid}
    if info.st_uid not in acceptable_owners or info.st_mode & 0o022:
        raise BootstrapError(f"pinned {label} executable is not root-owned read-only")


def _network_environment(environment: dict[str, str]) -> dict[str, str]:
    network = dict(environment)
    entries = (
        ("core.hooksPath", "/dev/null"),
        ("credential.https://github.com.helper", ""),
        ("credential.https://github.com.helper", f"!{TRUSTED_GH} auth git-credential"),
        ("credential.https://gist.github.com.helper", ""),
        (
            "credential.https://gist.github.com.helper",
            f"!{TRUSTED_GH} auth git-credential",
        ),
    )
    network["GIT_CONFIG_COUNT"] = str(len(entries))
    for index, (key, value) in enumerate(entries):
        network[f"GIT_CONFIG_KEY_{index}"] = key
        network[f"GIT_CONFIG_VALUE_{index}"] = value
    return network


def remote_default_identity(
    primary: Path,
    binding: RepositoryBinding,
    environment: dict[str, str],
) -> tuple[str, str]:
    result = _run_git(
        primary,
        "ls-remote",
        "--symref",
        "--exit-code",
        binding.origin_url,
        "HEAD",
        environment=_network_environment(environment),
        timeout=90,
    )
    rows = [row for row in result.stdout.splitlines() if row.strip()]
    if len(rows) != 2:
        raise BootstrapError(
            "remote default branch did not resolve to exactly one revision"
        )
    symbolic, first_separator, first_name = rows[0].partition("\t")
    target_ref = symbolic.removeprefix("ref: ")
    if (
        not symbolic.startswith("ref: ")
        or first_separator != "\t"
        or first_name != "HEAD"
        or DEFAULT_BRANCH_REF_RE.fullmatch(target_ref) is None
        or ".." in target_ref
        or "//" in target_ref
        or target_ref.endswith(("/", ".", ".lock"))
    ):
        raise BootstrapError("remote default branch returned malformed identity")
    oid, second_separator, second_name = rows[1].partition("\t")
    if (
        OID_RE.fullmatch(oid) is None
        or second_separator != "\t"
        or second_name != "HEAD"
    ):
        raise BootstrapError("remote default branch returned malformed identity")
    return target_ref, oid


def remote_default_oid(
    primary: Path,
    binding: RepositoryBinding,
    environment: dict[str, str],
) -> str:
    """Return the remote default OID for callers that do not need its ref."""
    return remote_default_identity(primary, binding, environment)[1]


def _read_floor(
    primary: Path,
    revision: str,
    trust_floor_path: str | PurePosixPath = TRUST_FLOOR_FILE,
) -> int:
    floor_path = PurePosixPath(str(trust_floor_path))
    if floor_path.is_absolute() or not floor_path.parts or ".." in floor_path.parts:
        raise BootstrapError("configured closeout trust-floor path is unsafe")
    try:
        result = _run_git(primary, "show", f"{revision}:{floor_path.as_posix()}")
    except BootstrapError as exc:
        raise BootstrapError("trusted revision has no valid trust floor") from exc
    try:
        payload = json.loads(result.stdout)
        epoch = payload["epoch"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BootstrapError("trusted revision has no valid trust floor") from exc
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise BootstrapError("trusted revision has no valid trust floor")
    return epoch


def validate_rollback(
    primary: Path,
    revision: str,
    remote_revision: str,
    *,
    rollback_reason: str,
    trust_floor_path: str | PurePosixPath = TRUST_FLOOR_FILE,
) -> None:
    if not rollback_reason.strip():
        raise BootstrapError("an explicit rollback reason is required")
    if OID_RE.fullmatch(revision) is None:
        raise BootstrapError("rollback revision must be 40 lowercase hex")
    ancestry = _run_git(
        primary,
        "merge-base",
        "--is-ancestor",
        revision,
        remote_revision,
        check=False,
    )
    if ancestry.returncode != 0:
        raise BootstrapError("rollback revision is not an ancestor of remote main")
    first_parent_history = _run_git(
        primary, "rev-list", "--first-parent", remote_revision
    ).stdout.splitlines()
    if revision not in first_parent_history:
        raise BootstrapError(
            "rollback revision is not on reviewed main first-parent history"
        )
    if _read_floor(primary, revision, trust_floor_path) < TRUST_FLOOR_EPOCH:
        raise BootstrapError("rollback revision is below the supported trust floor")


def _safe_tree_path(raw_path: bytes) -> PurePosixPath:
    try:
        decoded = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapError("toolchain tree contains a non-UTF-8 path") from exc
    path = PurePosixPath(decoded)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[:2] != ("scripts", "util")
        or len(path.parts) < 3
    ):
        raise BootstrapError("toolchain tree contains an unsafe path")
    return path


def _write_blob(destination: Path, source: BinaryIO | bytes, mode: int) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, mode)
    try:
        payload = source if isinstance(source, bytes) else source.read()
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    destination.chmod(mode)


def materialize_toolchain(primary: Path, revision: str, destination: Path) -> str:
    """Materialize regular blobs without archive/export-attribute processing."""

    listing = _run_git(
        primary,
        "ls-tree",
        "-rz",
        "--full-tree",
        revision,
        "--",
        "scripts/util",
        text=False,
    ).stdout
    entries: list[tuple[PurePosixPath, str, int]] = []
    seen: set[PurePosixPath] = set()
    for record in listing.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        try:
            raw_mode, object_type, raw_oid = metadata.decode("ascii").split()
            file_mode = int(raw_mode, 8)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BootstrapError("toolchain tree contains malformed metadata") from exc
        path = _safe_tree_path(raw_path) if separator else None
        if (
            path is None
            or object_type != "blob"
            or raw_mode not in {"100644", "100755"}
            or OID_RE.fullmatch(raw_oid) is None
        ):
            raise BootstrapError("toolchain tree must contain only regular blobs")
        if path in seen:
            raise BootstrapError("toolchain tree contains a duplicate path")
        seen.add(path)
        entries.append((path, raw_oid, 0o500 if file_mode & 0o111 else 0o400))
    if PurePosixPath("scripts/util/pr_closeout_impl.sh") not in seen:
        raise BootstrapError("trusted shell implementation is absent from toolchain")
    tree_oid = _run_git(primary, "rev-parse", f"{revision}:scripts/util").stdout.strip()
    if OID_RE.fullmatch(tree_oid) is None:
        raise BootstrapError("trusted toolchain tree identity is malformed")
    if destination.exists() or destination.is_symlink():
        raise BootstrapError("trusted snapshot destination already exists")
    try:
        destination.mkdir(mode=0o700)
        destination.chmod(0o700)
        for path, oid, mode in entries:
            target = destination.joinpath(*path.parts)
            current = destination
            for part in path.parts[:-1]:
                current /= part
                current.mkdir(mode=0o700, exist_ok=True)
                current.chmod(0o700)
            blob = _run_git(primary, "cat-file", "blob", oid, text=False).stdout
            _write_blob(target, blob, mode)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return tree_oid


def repair_primary(
    primary: Path,
    binding: RepositoryBinding,
    remote_default_ref: str,
    remote_revision: str,
    environment: dict[str, str],
    *,
    trust_floor_path: str | PurePosixPath = TRUST_FLOOR_FILE,
) -> None:
    """Re-park a clean reviewed primary through the current remote toolchain."""

    fetched = _run_git(
        primary,
        "fetch",
        "--no-tags",
        "--force",
        binding.origin_url,
        remote_default_ref,
        environment=_network_environment(environment),
        timeout=300,
    )
    if fetched.returncode != 0:
        raise BootstrapError("current remote main could not be fetched for repair")
    fetched_revision = _run_git(primary, "rev-parse", "--verify", "FETCH_HEAD").stdout.strip()
    if fetched_revision != remote_revision:
        raise BootstrapError("fetched repair target does not match current remote main")
    if read_repository_binding(primary, environment) != binding:
        raise BootstrapError("canonical Git configuration changed during primary repair")

    primary_head = _run_git(primary, "rev-parse", "HEAD").stdout.strip()
    validate_rollback(
        primary,
        primary_head,
        remote_revision,
        rollback_reason="repair clean canonical primary",
        trust_floor_path=trust_floor_path,
    )
    with tempfile.TemporaryDirectory(
        prefix="intelflo-primary-repair-", dir="/tmp", ignore_cleanup_errors=True
    ) as temporary:
        snapshot = Path(temporary) / "toolchain"
        materialize_toolchain(primary, remote_revision, snapshot)
        guard = snapshot / "scripts/util/worktree_guard.py"
        command = [
            TRUSTED_PYTHON,
            "-I",
            str(guard),
            "exec",
            "--worktree",
            str(primary),
            "--boundary",
            "pr-closeout-primary-repair",
            "--timeout",
            "300",
            "--",
            TRUSTED_BASH,
            "-p",
            "-c",
            """
set -euo pipefail
primary="$1"
expected="$2"
if [ -n "$(/usr/bin/git -C "$primary" status --porcelain=v1 --untracked-files=all)" ]; then
    echo "Primary repair refused — canonical primary is dirty" >&2
    exit 1
fi
/usr/bin/git -C "$primary" checkout -q --detach "$expected"
if [ "$(/usr/bin/git -C "$primary" rev-parse HEAD)" != "$expected" ] \
    || [ -n "$(/usr/bin/git -C "$primary" status --porcelain=v1 --untracked-files=all)" ]; then
    echo "Primary repair did not produce the exact clean remote-main checkout" >&2
    exit 1
fi
""",
            TRUSTED_BASH,
            str(primary),
            remote_revision,
        ]
        completed = subprocess.run(
            command,
            cwd=primary,
            env=_network_environment(environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=330,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise BootstrapError(f"canonical primary repair failed: {detail}")

    repaired_head = _run_git(primary, "rev-parse", "HEAD").stdout.strip()
    if repaired_head != remote_revision:
        raise BootstrapError("canonical primary repair did not reach current remote main")
    ensure_authority_clean(primary)
    if read_repository_binding(primary, environment) != binding:
        raise BootstrapError("canonical Git configuration changed during primary repair")


def implementation_command(
    snapshot: Path,
    forwarded: list[str],
    binding: RepositoryBinding,
) -> list[str]:
    """Bind the shell handoff to the launcher's already-validated repository."""

    return [
        TRUSTED_BASH,
        "-p",
        str(snapshot / "scripts/util/pr_closeout_impl.sh"),
        *forwarded,
        "--origin-url",
        binding.origin_url,
        "--git-config-sha256",
        binding.config_sha256,
    ]


def _record_rollback(
    primary: Path,
    *,
    revision: str,
    remote_revision: str,
    reason: str,
    tree_oid: str,
) -> None:
    directory = primary / ".audit/delivery-closeout"
    if directory.is_symlink():
        raise BootstrapError("rollback audit directory is not trusted")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "trusted-bootstrap-rollbacks.jsonl"
    if path.is_symlink():
        raise BootstrapError("rollback audit log is not trusted")
    payload = (
        json.dumps(
            {
                "recorded_at": datetime.now(UTC).isoformat(),
                "revision": revision,
                "remote_main": remote_revision,
                "reason": reason,
                "toolchain_tree": tree_oid,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inherited_lease_handoff(
    snapshot: Path, binding: DeliveryBinding
) -> tuple[tuple[int, ...], dict[str, str]]:
    """Carry only the bound lease, using the reviewed snapshot's guard contract."""
    if "INTELFLO_WORKTREE_LEASE_FD" not in os.environ:
        return (), {}
    # The launcher must not import executable code from the delivery checkout.
    # Unlike inherited_pass_fds(), this guard validates the descriptor's inode
    # against the registered worktree lock and verifies the exclusive flock.
    guard = runpy.run_path(str(snapshot / "scripts/util/worktree_guard.py"))
    lease = guard["_inherited_lease"](binding.git_dir / guard["LOCK_FILENAME"])
    if lease is None:
        raise BootstrapError("inherited worktree lease descriptor is invalid")
    return lease.pass_fds, lease.child_env()


def _bootstrap(
    argv: list[str],
    *,
    launcher_path: Path = Path(__file__),
    expected_launcher_path: Path = Path("scripts/util/pr_closeout.py"),
    trust_floor_path: str | PurePosixPath = TRUST_FLOOR_FILE,
    temp_prefix: str = "intelflo-closeout-",
) -> int:
    options, forwarded = parse_launcher_arguments(argv)
    default_launcher = Path("scripts/util/pr_closeout.py")
    primary = (
        _canonical_primary(launcher_path)
        if expected_launcher_path == default_launcher
        else _canonical_primary(launcher_path, expected_launcher_path)
    )
    delivery = Path.cwd().resolve()
    if not (delivery / ".git").is_file():
        raise BootstrapError(
            "invoke privileged closeout from the linked delivery worktree root"
        )
    environment = isolated_environment(dict(os.environ))
    for path, label in (
        (TRUSTED_BASH, "Bash"),
        (TRUSTED_GH, "GitHub CLI"),
        (TRUSTED_GIT, "Git"),
        (TRUSTED_PYTHON, "Python"),
    ):
        _validate_system_tool(path, label)
    binding = read_repository_binding(primary, environment)
    delivery_binding = verify_delivery_binding(primary, delivery)
    ensure_authority_clean(primary)
    remote_default_ref, remote_revision = remote_default_identity(
        primary, binding, environment
    )
    rebound = read_repository_binding(primary, environment)
    if rebound != binding:
        raise BootstrapError("canonical Git configuration changed before bootstrap")
    primary_head = _run_git(primary, "rev-parse", "HEAD").stdout.strip()
    if options.repair_primary:
        if primary_head != remote_revision:
            repair_primary(
                primary,
                binding,
                remote_default_ref,
                remote_revision,
                environment,
                trust_floor_path=trust_floor_path,
            )
            print(
                f"pr_closeout: canonical primary repaired at {remote_revision}; rerun closeout",
                file=sys.stderr,
            )
        else:
            print(
                f"pr_closeout: canonical primary already current at {remote_revision}",
                file=sys.stderr,
            )
        return 0
    if primary_head != remote_revision:
        # Credentialed closeout re-parks the primary before its fallible
        # post-merge tail; a mismatch here therefore remains a fail-closed
        # signal that recovery must repair the canonical trust root first.
        raise BootstrapError(
            "canonical primary must be clean and parked at current remote main"
        )
    revision = options.revision or remote_revision
    if options.revision:
        validate_rollback(
            primary,
            revision,
            remote_revision,
            rollback_reason=options.rollback_reason or "",
            trust_floor_path=trust_floor_path,
        )
    elif _read_floor(primary, revision, trust_floor_path) < TRUST_FLOOR_EPOCH:
        raise BootstrapError("remote main is below the supported trust floor")

    with tempfile.TemporaryDirectory(
        prefix=temp_prefix, dir="/tmp", ignore_cleanup_errors=True
    ) as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o700)
        snapshot = temporary_root / "toolchain"
        tree_oid = materialize_toolchain(primary, revision, snapshot)
        final_binding = read_repository_binding(primary, environment)
        if final_binding != binding:
            raise BootstrapError("canonical Git configuration changed during bootstrap")
        if options.revision:
            _record_rollback(
                primary,
                revision=revision,
                remote_revision=remote_revision,
                reason=options.rollback_reason or "",
                tree_oid=tree_oid,
            )
        environment = closeout_environment(
            environment,
            primary=primary,
            delivery=delivery,
            revision=revision,
            remote_default_ref=remote_default_ref,
            tree_oid=tree_oid,
        )
        print(
            f"pr_closeout: trusted revision {revision} toolchain {tree_oid}",
            file=sys.stderr,
        )
        lease_fds, lease_environment = inherited_lease_handoff(snapshot, delivery_binding)
        environment.update(lease_environment)
        completed = subprocess.run(
            implementation_command(snapshot, forwarded, binding),
            cwd=delivery,
            env=environment,
            check=False,
            pass_fds=lease_fds,
        )
        return completed.returncode


def main(
    argv: list[str] | None = None,
    *,
    profile: Any | None = None,
    launcher_path: Path | None = None,
    trust_floor_path: str | PurePosixPath | None = None,
) -> int:
    if not sys.flags.isolated:
        print(
            "pr_closeout: invoke with `/usr/bin/python3 -I "
            "/absolute/primary/scripts/util/pr_closeout.py`",
            file=sys.stderr,
        )
        return 2
    try:
        settings = CloseoutSettings.from_profile(profile) if profile is not None else None
        expected_launcher = (
            settings.launcher_path
            if settings is not None
            else Path("scripts/util/pr_closeout.py")
        )
        actual_launcher = launcher_path or (
            Path(profile.root).resolve() / expected_launcher
            if profile is not None
            else Path(__file__)
        )
        configured_floor = trust_floor_path or (
            settings.trust_floor_path if settings is not None else TRUST_FLOOR_FILE
        )
        return _bootstrap(
            list(sys.argv[1:] if argv is None else argv),
            launcher_path=Path(actual_launcher),
            expected_launcher_path=expected_launcher,
            trust_floor_path=configured_floor,
            temp_prefix=(
                settings.temp_prefix if settings is not None else "intelflo-closeout-"
            ),
        )
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        print(f"pr_closeout: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
