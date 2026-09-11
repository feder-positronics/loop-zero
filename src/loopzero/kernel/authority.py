#!/usr/bin/env python3
"""Ephemeral Ed25519 authority for dispatcher-authored terminal records."""

from __future__ import annotations

from .settings import settings

import base64
import binascii
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping, Sequence


from .canonical import canonical_record_digest
from .trusted_exec import TrustedExecutableError, system_executable

LEGACY_AUTHORITY_SCHEME = "dispatch-terminal-ed25519-v1"
AUTHORITY_SCHEME = "dispatch-terminal-ed25519-v2"
SUPPORTED_AUTHORITY_SCHEMES = frozenset({LEGACY_AUTHORITY_SCHEME, AUTHORITY_SCHEME})
COORDINATOR_AUTHORITY_SCHEME = "dispatch-coordinator-ed25519-v2"
LEGACY_COORDINATOR_AUTHORITY_SCHEME = "dispatch-coordinator-ssh-ed25519-v1"
LEGACY_COORDINATOR_SIGNATURE_NAMESPACE = settings.legacy_signature_namespace
LEGACY_COORDINATOR_PUBLIC_KEY = settings.legacy_public_key
COORDINATOR_KEY_FILENAME = "coordinator-ed25519.pem"
COORDINATOR_LOCK_FILENAME = settings.coordinator_lock_name
MAX_COORDINATOR_KEY_BYTES = 4096
MAX_COORDINATOR_LEDGER_STATE_BYTES = 1024 * 1024
COORDINATOR_LEDGER_DIRECTORY = "ledgers"
COORDINATOR_LEDGER_LOCK_FILENAME = settings.ledger_state_lock_name
_LEDGER_BINDING_RE = re.compile(r"[0-9a-f]{64}")
AUTHORITY_PROVIDER_TIMEOUT_S = 10.0
PROOF_FIELD = "terminal_authority_proof"
PTRACE_SCOPE_PATH = Path("/proc/sys/kernel/yama/ptrace_scope")
GUARDIAN_COORDINATOR_PUBLIC_KEY_PATH = Path(
    "/run/guardian-authority/coordinator-public-key.der"
)
AuthorityKind = Literal["dispatcher", "coordinator"]


class TerminalAuthorityError(RuntimeError):
    """A terminal record has no valid dispatcher/coordinator authority proof."""


class TerminalAuthorityOperationalError(TerminalAuthorityError):
    """The verification provider failed transiently; the proof was not judged.

    Consumers that skip or reject records on TerminalAuthorityError must not
    treat this subclass as durable evidence of an invalid proof (for example,
    by caching the rejection).
    """


@lru_cache(maxsize=1)
def _openssl() -> Path:
    try:
        return system_executable("openssl")
    except TrustedExecutableError as exc:
        raise TerminalAuthorityError("trusted Ed25519 provider is unavailable") from exc


def _run_openssl(
    arguments: list[str],
    *,
    input_bytes: bytes,
    inherited: tuple[int, ...] = (),
) -> bytes:
    try:
        completed = subprocess.run(
            [str(_openssl()), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
            close_fds=True,
            pass_fds=inherited,
            env={"LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
            timeout=AUTHORITY_PROVIDER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise TerminalAuthorityOperationalError(
            "Ed25519 authority operation timed out"
        ) from exc
    if completed.returncode != 0:
        raise TerminalAuthorityError("Ed25519 authority operation failed")
    return completed.stdout


def _with_read_descriptor(payload: bytes, operation):
    if not hasattr(os, "memfd_create"):
        with tempfile.TemporaryFile(prefix=settings.temp_prefix + "dispatch-authority-") as handle:
            handle.write(payload)
            handle.flush()
            handle.seek(0)
            return operation(handle.fileno())
    read_fd = os.memfd_create(settings.temp_prefix + "dispatch-authority", os.MFD_CLOEXEC)
    try:
        os.write(read_fd, payload)
        os.lseek(read_fd, 0, os.SEEK_SET)
        return operation(read_fd)
    finally:
        os.close(read_fd)


def _canonical_payload(
    record: Mapping[str, object],
    *,
    authority_kind: AuthorityKind | None = None,
    scheme: str | None = None,
) -> bytes:
    payload = {key: value for key, value in record.items() if key != PROOF_FIELD}
    if authority_kind is not None:
        payload = {
            "authority": {
                "scheme": scheme,
                "authority_kind": authority_kind,
            },
            "record": payload,
        }
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise TerminalAuthorityError("terminal authority payload is invalid") from exc


def _decode(value: object, *, label: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise TerminalAuthorityError(f"terminal authority {label} is invalid")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TerminalAuthorityError(f"terminal authority {label} is invalid") from exc


def _key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _coordinator_key_id(public_key: bytes) -> str:
    return _key_id(public_key)


def _legacy_coordinator_key_id(public_key: str) -> str:
    return hashlib.sha256(public_key.encode("ascii")).hexdigest()


def _require_process_memory_isolation() -> None:
    """Refuse a key when same-UID children could inspect dispatcher memory."""
    try:
        scope = int(PTRACE_SCOPE_PATH.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise TerminalAuthorityError(
            "kernel process memory isolation is unavailable"
        ) from exc
    if scope < 1:
        raise TerminalAuthorityError("kernel process memory isolation is unavailable")


@dataclass(frozen=True)
class TerminalAuthority:
    """One process-local signing key; private bytes never enter records or repr."""

    public_key: bytes = field(repr=False)
    _private_key: bytes = field(repr=False)

    @classmethod
    def generate(cls) -> TerminalAuthority:
        _require_process_memory_isolation()
        private_key = _run_openssl(
            ["genpkey", "-algorithm", "Ed25519"], input_bytes=b""
        )
        if b"PRIVATE KEY" not in private_key or len(private_key) > 4096:
            raise TerminalAuthorityError("Ed25519 private key generation failed")

        def derive(read_fd: int) -> bytes:
            return _run_openssl(
                [
                    "pkey",
                    "-in",
                    f"/proc/self/fd/{read_fd}",
                    "-pubout",
                    "-outform",
                    "DER",
                ],
                input_bytes=b"",
                inherited=(read_fd,),
            )

        public_key = _with_read_descriptor(private_key, derive)
        if not public_key or len(public_key) > 1024:
            raise TerminalAuthorityError("Ed25519 public key generation failed")
        return cls(public_key=public_key, _private_key=private_key)

    def registration(self) -> dict[str, str]:
        return {
            "scheme": AUTHORITY_SCHEME,
            "key_id": _key_id(self.public_key),
            "public_key": base64.b64encode(self.public_key).decode("ascii"),
        }

    def seal(
        self,
        record: Mapping[str, object],
        *,
        authority_kind: AuthorityKind,
        include_public_key: bool = False,
    ) -> dict[str, object]:
        return self._seal(
            record,
            authority_kind=authority_kind,
            include_public_key=include_public_key,
            scheme=AUTHORITY_SCHEME,
        )

    def _seal(
        self,
        record: Mapping[str, object],
        *,
        authority_kind: AuthorityKind,
        include_public_key: bool = False,
        scheme: str,
    ) -> dict[str, object]:
        """Seal one record; legacy scheme access is verification-test-only."""
        if PROOF_FIELD in record:
            raise TerminalAuthorityError("terminal authority proof already exists")
        if scheme not in SUPPORTED_AUTHORITY_SCHEMES:
            raise TerminalAuthorityError("terminal authority scheme is invalid")
        payload = _canonical_payload(
            record,
            authority_kind=(authority_kind if scheme == AUTHORITY_SCHEME else None),
            scheme=scheme,
        )

        def sign(key_fd: int) -> bytes:
            def sign_payload(payload_fd: int) -> bytes:
                return _run_openssl(
                    [
                        "pkeyutl",
                        "-sign",
                        "-rawin",
                        "-inkey",
                        f"/proc/self/fd/{key_fd}",
                        "-in",
                        f"/proc/self/fd/{payload_fd}",
                    ],
                    input_bytes=b"",
                    inherited=(key_fd, payload_fd),
                )

            return _with_read_descriptor(payload, sign_payload)

        signature = _with_read_descriptor(self._private_key, sign)
        proof = {
            "scheme": scheme,
            "authority_kind": authority_kind,
            "key_id": _key_id(self.public_key),
            "signature": base64.b64encode(signature).decode("ascii"),
        }
        if include_public_key:
            proof["public_key"] = base64.b64encode(self.public_key).decode("ascii")
        return {**record, PROOF_FIELD: proof}


@dataclass(frozen=True)
class CoordinatorAuthority:
    """Host signer whose persistent private key stays outside worker mounts."""

    public_key: bytes = field(repr=False)
    _private_key: bytes = field(repr=False)

    @classmethod
    def from_local_state(cls) -> CoordinatorAuthority:
        _require_process_memory_isolation()
        private_key = _coordinator_private_key(create=True)
        return cls(
            public_key=_public_key_from_private(private_key),
            _private_key=private_key,
        )

    def seal(
        self,
        record: Mapping[str, object],
        *,
        authority_kind: AuthorityKind,
        include_public_key: bool = False,
    ) -> dict[str, object]:
        del include_public_key
        if authority_kind != "coordinator":
            raise TerminalAuthorityError(
                "coordinator authority cannot seal dispatcher records"
            )
        if PROOF_FIELD in record:
            raise TerminalAuthorityError("terminal authority proof already exists")
        payload = _canonical_payload(
            record,
            authority_kind="coordinator",
            scheme=COORDINATOR_AUTHORITY_SCHEME,
        )
        signature = _sign_ed25519(self._private_key, payload)
        return {
            **record,
            PROOF_FIELD: {
                "scheme": COORDINATOR_AUTHORITY_SCHEME,
                "authority_kind": "coordinator",
                "key_id": _coordinator_key_id(self.public_key),
                "public_key": base64.b64encode(self.public_key).decode("ascii"),
                "signature": base64.b64encode(signature).decode("ascii"),
            },
        }


def _coordinator_state_directory() -> Path:
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise TerminalAuthorityError("coordinator account home is unavailable") from exc
    if not account_home.is_absolute():
        raise TerminalAuthorityError("coordinator account home is unsafe")
    return settings.account_state_root(account_home) / "dispatch-authority"


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise TerminalAuthorityError("coordinator state is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise TerminalAuthorityError("coordinator state is unsafe")
    return path


def _coordinator_ledger_directory() -> Path:
    return _private_directory(
        _private_directory(_coordinator_state_directory())
        / COORDINATOR_LEDGER_DIRECTORY
    )


def _coordinator_ledger_state_path(repository_binding: str) -> Path:
    if _LEDGER_BINDING_RE.fullmatch(repository_binding) is None:
        raise TerminalAuthorityError("coordinator ledger binding is invalid")
    return _coordinator_ledger_directory() / f"{repository_binding}.json"


def _read_coordinator_ledger_state(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TerminalAuthorityError("coordinator ledger state is unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o777 != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > MAX_COORDINATOR_LEDGER_STATE_BYTES
        ):
            raise TerminalAuthorityError("coordinator ledger state is unsafe")
        payload = bytearray()
        while len(payload) <= MAX_COORDINATOR_LEDGER_STATE_BYTES:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_COORDINATOR_LEDGER_STATE_BYTES:
            raise TerminalAuthorityError("coordinator ledger state is unsafe")
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TerminalAuthorityError("coordinator ledger state is invalid") from exc
    if not isinstance(parsed, dict):
        raise TerminalAuthorityError("coordinator ledger state is invalid")
    return parsed


def load_coordinator_ledger_state(
    repository_binding: str,
) -> dict[str, object] | None:
    """Read one protected per-repository ledger head without creating it."""
    path = _coordinator_ledger_state_path(repository_binding)
    try:
        state = _read_coordinator_ledger_state(path)
    except FileNotFoundError:
        return None
    _validate_coordinator_ledger_state(state, repository_binding)
    return state


def _validate_coordinator_ledger_state(
    state: Mapping[str, object], repository_binding: str
) -> int:
    if state.get("repository_binding") != repository_binding:
        raise TerminalAuthorityError("coordinator ledger binding does not match")
    generation = state.get("generation")
    if (
        state.get("scheme") != "dispatch-authority-ledger-host-state-v1"
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise TerminalAuthorityError("coordinator ledger state is invalid")
    return generation


def _open_coordinator_ledger_lock(directory: Path) -> int:
    lock_path = directory / COORDINATOR_LEDGER_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TerminalAuthorityError("coordinator ledger lock is unavailable") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o777 != 0o600
    ):
        os.close(descriptor)
        raise TerminalAuthorityError("coordinator ledger lock is unsafe")
    return descriptor


def _fsync_private_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise TerminalAuthorityError("coordinator state sync is unavailable") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise TerminalAuthorityError("coordinator state sync failed") from exc
    finally:
        os.close(descriptor)


def commit_coordinator_ledger_state(
    repository_binding: str,
    state: Mapping[str, object],
    *,
    expected_generation: int | None,
    allow_same_generation_reseal: bool = False,
    allow_same_generation_append: bool = False,
    appended_record_count: int = 1,
) -> None:
    """Atomically advance one protected ledger head with generation CAS."""
    path = _coordinator_ledger_state_path(repository_binding)
    if state.get("repository_binding") != repository_binding:
        raise TerminalAuthorityError("coordinator ledger binding does not match")
    generation = state.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise TerminalAuthorityError("coordinator ledger generation is invalid")
    if allow_same_generation_reseal and allow_same_generation_append:
        raise TerminalAuthorityError("coordinator ledger state update is invalid")
    if (
        isinstance(appended_record_count, bool)
        or not isinstance(appended_record_count, int)
        or appended_record_count < 1
        or (not allow_same_generation_append and appended_record_count != 1)
    ):
        raise TerminalAuthorityError("coordinator ledger state update is invalid")
    same_generation = allow_same_generation_reseal or allow_same_generation_append
    expected_next_generation = (
        expected_generation
        if same_generation
        else expected_generation + 1
        if expected_generation is not None
        else None
    )
    if expected_generation is not None and (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
        or generation != expected_next_generation
    ):
        raise TerminalAuthorityError("coordinator ledger generation is invalid")
    if expected_generation is None and generation != 1:
        raise TerminalAuthorityError("coordinator ledger generation is invalid")
    if state.get("scheme") != "dispatch-authority-ledger-host-state-v1":
        raise TerminalAuthorityError("coordinator ledger state is invalid")
    try:
        payload = (
            json.dumps(
                dict(state),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise TerminalAuthorityError("coordinator ledger state is invalid") from exc
    if not payload or len(payload) > MAX_COORDINATOR_LEDGER_STATE_BYTES:
        raise TerminalAuthorityError("coordinator ledger state is invalid")

    directory = path.parent
    lock_descriptor = _open_coordinator_ledger_lock(directory)
    temporary: Path | None = None
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        try:
            current = _read_coordinator_ledger_state(path)
        except FileNotFoundError:
            current = None
        current_generation = (
            _validate_coordinator_ledger_state(current, repository_binding)
            if current is not None
            else None
        )
        if expected_generation is None:
            if current is not None:
                raise TerminalAuthorityError("coordinator ledger generation changed")
        elif current_generation != expected_generation:
            raise TerminalAuthorityError("coordinator ledger generation changed")
        if allow_same_generation_reseal:
            assert current is not None
            immutable_current = dict(current)
            immutable_next = dict(state)
            immutable_current.pop("archive_stat_seals", None)
            immutable_next.pop("archive_stat_seals", None)
            if immutable_current != immutable_next:
                raise TerminalAuthorityError(
                    "coordinator ledger reseal changed logical head"
                )
        if allow_same_generation_append:
            assert current is not None
            immutable_current = dict(current)
            immutable_next = dict(state)
            active_fields = {
                "active_record_count",
                "active_records_sha256",
                "active_byte_size",
            }
            for field_name in active_fields:
                immutable_current.pop(field_name, None)
                immutable_next.pop(field_name, None)
            current_count = current.get("active_record_count")
            next_count = state.get("active_record_count")
            current_size = current.get("active_byte_size")
            next_size = state.get("active_byte_size")
            if (
                immutable_current != immutable_next
                or not isinstance(current_count, int)
                or not isinstance(next_count, int)
                or next_count != current_count + appended_record_count
                or not isinstance(current_size, int)
                or not isinstance(next_size, int)
                or next_size <= current_size
                or state.get("active_records_sha256")
                == current.get("active_records_sha256")
            ):
                raise TerminalAuthorityError(
                    "coordinator ledger append changed logical head"
                )

        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=f".{repository_binding}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
        _fsync_private_directory(directory)
        if _read_coordinator_ledger_state(path) != dict(state):
            raise TerminalAuthorityError("coordinator ledger state did not persist")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(lock_descriptor)


def _read_private_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TerminalAuthorityError("coordinator private key is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o777 != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > MAX_COORDINATOR_KEY_BYTES
        ):
            raise TerminalAuthorityError("coordinator private key is unsafe")
        chunks: list[bytes] = []
        remaining = MAX_COORDINATOR_KEY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        private_key = b"".join(chunks)
    except OSError as exc:
        raise TerminalAuthorityError("coordinator private key is unavailable") from exc
    finally:
        os.close(descriptor)
    if not private_key or len(private_key) > MAX_COORDINATOR_KEY_BYTES:
        raise TerminalAuthorityError("coordinator private key is unsafe")
    if b"PRIVATE KEY" not in private_key:
        raise TerminalAuthorityError("coordinator private key is invalid")
    return private_key


def _coordinator_private_key(*, create: bool) -> bytes:
    directory = _private_directory(_coordinator_state_directory())
    key_path = directory / COORDINATOR_KEY_FILENAME
    lock_path = directory / COORDINATOR_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TerminalAuthorityError("coordinator key lock is unavailable") from exc
    try:
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.getuid()
            or lock_metadata.st_mode & 0o777 != 0o600
        ):
            raise TerminalAuthorityError("coordinator key lock is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            return _read_private_key(key_path)
        except FileNotFoundError as exc:
            if not create:
                raise TerminalAuthorityError(
                    "coordinator trust root is unavailable"
                ) from exc
        private_key = _run_openssl(
            ["genpkey", "-algorithm", "Ed25519"], input_bytes=b""
        )
        if (
            b"PRIVATE KEY" not in private_key
            or len(private_key) > MAX_COORDINATOR_KEY_BYTES
        ):
            raise TerminalAuthorityError("coordinator private key generation failed")
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(private_key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
            temporary.replace(key_path)
        finally:
            temporary.unlink(missing_ok=True)
        return _read_private_key(key_path)
    finally:
        os.close(lock_fd)


def _public_key_from_private(private_key: bytes) -> bytes:
    def derive(read_fd: int) -> bytes:
        return _run_openssl(
            [
                "pkey",
                "-in",
                f"/proc/self/fd/{read_fd}",
                "-pubout",
                "-outform",
                "DER",
            ],
            input_bytes=b"",
            inherited=(read_fd,),
        )

    public_key = _with_read_descriptor(private_key, derive)
    if not public_key or len(public_key) > 1024:
        raise TerminalAuthorityError("coordinator public key is invalid")
    return public_key


@lru_cache(maxsize=4)
def _trusted_public_key_from_file(
    key_path: str,
    device: int,
    inode: int,
    mode: int,
    owner: int,
    size: int,
    modified_ns: int,
    changed_ns: int,
) -> bytes:
    """Cache only derived public material, keyed by exact private-file identity."""
    del device, inode, mode, owner, size, modified_ns, changed_ns
    return _public_key_from_private(_read_private_key(Path(key_path)))


def _guardian_projection_parent_is_safe(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_mode & 0o022 == 0
    )


def _guardian_projection_is_read_only(descriptor: int) -> bool:
    """Bind the kernel-enforced read-only proof to the opened trust-root file."""
    try:
        return bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _trusted_coordinator_public_key() -> bytes:
    _require_process_memory_isolation()
    guardian_projection = GUARDIAN_COORDINATOR_PUBLIC_KEY_PATH
    if (
        os.environ.get(settings.env("GUARDIAN_SANDBOX_BOUNDARY"))
        in {"network-denied", "host-network"}
        and guardian_projection.is_file()
    ):
        if not _guardian_projection_parent_is_safe(guardian_projection.parent):
            raise TerminalAuthorityError(
                "Guardian coordinator public key projection parent is unsafe"
            )
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(guardian_projection, flags)
            try:
                metadata = os.fstat(descriptor)
                if not _guardian_projection_is_read_only(descriptor):
                    raise TerminalAuthorityError(
                        "Guardian coordinator public key projection is not read-only"
                    )
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o777 != 0o600
                    or metadata.st_size <= 0
                    or metadata.st_size > 2048
                ):
                    raise TerminalAuthorityError(
                        "Guardian coordinator public key is unsafe"
                    )
                public_key = os.read(descriptor, 2049)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise TerminalAuthorityError(
                "Guardian coordinator public key is unavailable"
            ) from exc
        if not public_key or len(public_key) > 2048:
            raise TerminalAuthorityError("Guardian coordinator public key is invalid")
        return public_key
    key_path = (
        _private_directory(_coordinator_state_directory()) / COORDINATOR_KEY_FILENAME
    )
    try:
        metadata = key_path.lstat()
    except FileNotFoundError as exc:
        raise TerminalAuthorityError("coordinator trust root is unavailable") from exc
    except OSError as exc:
        raise TerminalAuthorityError("coordinator private key is unavailable") from exc
    return _trusted_public_key_from_file(
        str(key_path),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@lru_cache(maxsize=4096)
def _legacy_coordinator_signature_is_valid(signature: bytes, payload: bytes) -> bool:
    """Verify the retired SSH-agent proof used before the v2 host cutover."""
    try:
        ssh_keygen = system_executable("ssh-keygen")
    except TrustedExecutableError as exc:
        raise TerminalAuthorityError("trusted SSH verifier is unavailable") from exc
    with tempfile.TemporaryDirectory(prefix=settings.temp_prefix + "coordinator-verify-") as raw:
        directory = Path(raw)
        allowed_path = directory / "allowed-signers"
        signature_path = directory / "signature"
        allowed_path.write_text(
            f"coordinator {LEGACY_COORDINATOR_PUBLIC_KEY}\n", encoding="ascii"
        )
        signature_path.write_bytes(signature)
        try:
            completed = subprocess.run(
                [
                    str(ssh_keygen),
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed_path),
                    "-I",
                    "coordinator",
                    "-n",
                    LEGACY_COORDINATOR_SIGNATURE_NAMESPACE,
                    "-s",
                    str(signature_path),
                ],
                input=payload,
                capture_output=True,
                check=False,
                close_fds=True,
                env={"LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
                timeout=AUTHORITY_PROVIDER_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise TerminalAuthorityOperationalError(
                "legacy coordinator verification timed out"
            ) from exc
        if completed.returncode == 0:
            return True
        if completed.returncode == 1 or (
            completed.returncode == 255
            and completed.stderr.startswith(b"Signature verification failed:")
        ):
            return False
        # Anything other than success or a documented signature mismatch is a
        # provider/runtime failure: the proof was not judged, so the rejection
        # must stay retryable and uncacheable.
        raise TerminalAuthorityOperationalError(
            "legacy coordinator verification provider failed"
        )


def verify_legacy_coordinator_authority(record: Mapping[str, object]) -> None:
    """Verify one retired SSH coordinator proof for cutover adoption only."""
    proof = record.get(PROOF_FIELD)
    if (
        not LEGACY_COORDINATOR_PUBLIC_KEY
        or not isinstance(proof, dict)
        or proof.get("scheme") != LEGACY_COORDINATOR_AUTHORITY_SCHEME
        or proof.get("authority_kind") != "coordinator"
        or proof.get("public_key") != LEGACY_COORDINATOR_PUBLIC_KEY
        or proof.get("key_id")
        != _legacy_coordinator_key_id(LEGACY_COORDINATOR_PUBLIC_KEY)
    ):
        raise TerminalAuthorityError("legacy coordinator authority proof is invalid")
    signature = _decode(proof.get("signature"), label="signature", maximum=8192)
    payload = _canonical_payload(
        record,
        authority_kind="coordinator",
        scheme=LEGACY_COORDINATOR_AUTHORITY_SCHEME,
    )
    if not _legacy_coordinator_signature_is_valid(signature, payload):
        raise TerminalAuthorityError(
            "legacy coordinator authority signature is invalid"
        )


def _sign_ed25519(private_key: bytes, payload: bytes) -> bytes:
    def sign(key_fd: int) -> bytes:
        def sign_payload(payload_fd: int) -> bytes:
            return _run_openssl(
                [
                    "pkeyutl",
                    "-sign",
                    "-rawin",
                    "-inkey",
                    f"/proc/self/fd/{key_fd}",
                    "-in",
                    f"/proc/self/fd/{payload_fd}",
                ],
                input_bytes=b"",
                inherited=(key_fd, payload_fd),
            )

        return _with_read_descriptor(payload, sign_payload)

    return _with_read_descriptor(private_key, sign)


def _validated_registration(
    registration: Mapping[str, object],
) -> tuple[bytes, str]:
    scheme = registration.get("scheme")
    if not isinstance(scheme, str) or scheme not in SUPPORTED_AUTHORITY_SCHEMES:
        raise TerminalAuthorityError("terminal authority scheme is invalid")
    public_key = _decode(
        registration.get("public_key"), label="public key", maximum=2048
    )
    key_id = registration.get("key_id")
    if not isinstance(key_id, str) or key_id != _key_id(public_key):
        raise TerminalAuthorityError("terminal authority key is invalid")
    return public_key, scheme


@lru_cache(maxsize=4096)
def _signature_is_valid(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Bound repeated ledger projections to one verification per exact proof."""

    def verify_key(key_fd: int) -> bool:
        def verify_payload(payload_fd: int) -> bool:
            def verify_signature(signature_fd: int) -> bool:
                try:
                    completed = subprocess.run(
                        [
                            str(_openssl()),
                            "pkeyutl",
                            "-verify",
                            "-rawin",
                            "-pubin",
                            "-keyform",
                            "DER",
                            "-inkey",
                            f"/proc/self/fd/{key_fd}",
                            "-in",
                            f"/proc/self/fd/{payload_fd}",
                            "-sigfile",
                            f"/proc/self/fd/{signature_fd}",
                        ],
                        input=b"",
                        capture_output=True,
                        check=False,
                        close_fds=True,
                        pass_fds=(key_fd, payload_fd, signature_fd),
                        env={"LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
                        timeout=AUTHORITY_PROVIDER_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired as exc:
                    # Exceptions are not retained by lru_cache. A transient
                    # provider stall must retry rather than poison this proof.
                    raise TerminalAuthorityOperationalError(
                        "terminal authority provider timed out"
                    ) from exc
                if completed.returncode == 0:
                    return True
                if completed.returncode == 1:
                    return False
                # Only OpenSSL's documented verification-mismatch result is
                # durable invalidity. Provider/runtime failures must remain
                # retryable and therefore must not enter the LRU cache.
                raise TerminalAuthorityOperationalError(
                    "terminal authority provider failed"
                )

            return _with_read_descriptor(signature, verify_signature)

        return _with_read_descriptor(payload, verify_payload)

    return _with_read_descriptor(public_key, verify_key)


def verify_terminal_authority(
    record: Mapping[str, object],
    *,
    registration: Mapping[str, object] | None,
    expected_kind: AuthorityKind,
    coordinator_public_key: bytes | None = None,
) -> None:
    proof = record.get(PROOF_FIELD)
    if not isinstance(proof, dict):
        raise TerminalAuthorityError("terminal authority proof is missing")
    proof_scheme = proof.get("scheme")
    if proof_scheme == COORDINATOR_AUTHORITY_SCHEME:
        if (
            expected_kind != "coordinator"
            or registration is not None
            or proof.get("authority_kind") != "coordinator"
        ):
            raise TerminalAuthorityError("coordinator authority kind is invalid")
        public_key = _decode(proof.get("public_key"), label="public key", maximum=2048)
        if coordinator_public_key is None:
            try:
                trusted_public_key = _trusted_coordinator_public_key()
            except TerminalAuthorityOperationalError as exc:
                raise TerminalAuthorityOperationalError(
                    "coordinator authority key is unavailable"
                ) from exc
            except TerminalAuthorityError as exc:
                raise TerminalAuthorityError(
                    "coordinator authority key is unavailable"
                ) from exc
        else:
            trusted_public_key = coordinator_public_key
        if public_key != trusted_public_key or proof.get(
            "key_id"
        ) != _coordinator_key_id(trusted_public_key):
            raise TerminalAuthorityError("coordinator authority key is invalid")
        signature = _decode(proof.get("signature"), label="signature", maximum=8192)
        payload = _canonical_payload(
            record,
            authority_kind="coordinator",
            scheme=COORDINATOR_AUTHORITY_SCHEME,
        )
        if not _signature_is_valid(public_key, signature, payload):
            raise TerminalAuthorityError("coordinator authority signature is invalid")
        return
    if (
        not isinstance(proof_scheme, str)
        or proof_scheme not in SUPPORTED_AUTHORITY_SCHEMES
    ):
        raise TerminalAuthorityError("terminal authority scheme is invalid")
    if proof_scheme == LEGACY_AUTHORITY_SCHEME and expected_kind != "dispatcher":
        raise TerminalAuthorityError(
            "legacy terminal authority is valid only for a dispatcher"
        )
    if expected_kind == "coordinator":
        raise TerminalAuthorityError("coordinator authority scheme is invalid")
    if proof.get("authority_kind") != expected_kind:
        raise TerminalAuthorityError("terminal authority kind is invalid")
    if proof_scheme == LEGACY_AUTHORITY_SCHEME and registration is None:
        raise TerminalAuthorityError(
            "legacy terminal authority requires a registered dispatcher key"
        )
    if registration is None:
        registration = proof
    public_key, registration_scheme = _validated_registration(registration)
    if proof_scheme != registration_scheme:
        raise TerminalAuthorityError("terminal authority scheme does not match")
    if proof.get("key_id") != _key_id(public_key):
        raise TerminalAuthorityError("terminal authority key does not match")
    if expected_kind == "dispatcher" and "public_key" in proof:
        raise TerminalAuthorityError("dispatcher proof may not replace its key")
    signature = _decode(proof.get("signature"), label="signature", maximum=1024)
    payload = _canonical_payload(
        record,
        authority_kind=(expected_kind if proof_scheme == AUTHORITY_SCHEME else None),
        scheme=proof_scheme,
    )

    if not _signature_is_valid(public_key, signature, payload):
        raise TerminalAuthorityError("terminal authority signature is invalid")


def _attempt_owner_record_digests(
    records: Sequence[Mapping[str, object]],
    *,
    task_id: object,
    attempt_index: object,
    before: Mapping[str, object],
) -> frozenset[str]:
    """Digest the ledger's `attempt-owner` rows for one attempt.

    Only rows recorded before the attempt's first settlement count: the ledger
    is append-only and settlements are first-writer-wins per attempt, so a row
    appended after `before` (or after any settlement of the same attempt) can
    never have been observed by it. Consumers may hold re-projected copies, so
    the cutoff is matched by content, not object identity.
    """
    digests: set[str] = set()
    for record in records:
        same_attempt = (
            record.get("task_id") == task_id
            and not isinstance(record.get("attempt_index"), bool)
            and record.get("attempt_index") == attempt_index
        )
        if record is before or (
            same_attempt
            and record.get("type") in {"attempt-abort", "attempt-terminal"}
        ):
            break
        if same_attempt and record.get("type") == "attempt-owner":
            digests.add(canonical_record_digest(record))
    return frozenset(digests)


def authenticated_gone_owner_abort(
    record: Mapping[str, object],
    start: Mapping[str, object],
    authenticated_coordinator_ids: frozenset[int],
    records: Sequence[Mapping[str, object]],
) -> bool:
    """Accept only a host-signed, exact-start failure settlement after owner loss.

    The proof's process half is checked against the ledger, not trusted: it
    must carry the start's controller identity and a non-empty digest list
    that names every `attempt-owner` row for this attempt recorded before the
    settlement. Rows appended later are never consulted and rows pruned by
    retention cannot revoke an accepted settlement, so no ledger writer can
    reopen a host-signed failure by adding or removing owner rows.
    """
    proof = record.get("gone_owner_proof")
    if (
        id(record) not in authenticated_coordinator_ids
        or record.get("type") != "attempt-abort"
        or record.get("status") != "infrastructure-failure"
        or record.get("failure_class") != "operator-terminated"
        or record.get("settlement_evidence") != "gone-owner-v1"
        or not isinstance(proof, dict)
        or proof.get("version") != 1
        or proof.get("start_digest") != canonical_record_digest(start)
        or not all(
            record.get(field) == start.get(field)
            for field in (
                "task_id",
                "attempt_index",
                "run_id",
                "work_unit_id",
                "root_work_unit_id",
                "worktree",
                "task_contract_hash",
            )
        )
    ):
        return False
    process, lease, worktree = (
        proof.get(key) for key in ("process", "lease", "worktree")
    )
    controller_identity = start.get("controller_identity")
    if not isinstance(process, dict) or not isinstance(controller_identity, dict):
        return False
    recorded_digests = process.get("owner_record_digests")
    if (
        process.get("state") != "gone"
        or process.get("controller_identity") != controller_identity
        or not isinstance(recorded_digests, list)
        or not recorded_digests
        or not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
            for item in recorded_digests
        )
        or not _attempt_owner_record_digests(
            records,
            task_id=start.get("task_id"),
            attempt_index=start.get("attempt_index"),
            before=record,
        )
        <= set(recorded_digests)
    ):
        return False
    return (
        isinstance(lease, dict)
        and lease.get("state") in {"acquired", "absent"}
        and isinstance(worktree, dict)
        and worktree.get("state") in {"present", "absent"}
        and worktree.get("path") == start.get("worktree")
        and (lease.get("state") != "absent" or worktree.get("state") == "absent")
    )
