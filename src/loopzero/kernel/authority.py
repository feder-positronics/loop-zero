#!/usr/bin/env python3
"""Ephemeral Ed25519 authority for dispatcher-authored terminal records."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping

# Sibling imports must survive PYTHONSAFEPATH=1 (job.sh) and python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trusted_executable import TrustedExecutableError, system_executable

LEGACY_AUTHORITY_SCHEME = "dispatch-terminal-ed25519-v1"
AUTHORITY_SCHEME = "dispatch-terminal-ed25519-v2"
SUPPORTED_AUTHORITY_SCHEMES = frozenset({LEGACY_AUTHORITY_SCHEME, AUTHORITY_SCHEME})
COORDINATOR_AUTHORITY_SCHEME = "dispatch-coordinator-ed25519-v2"
LEGACY_COORDINATOR_AUTHORITY_SCHEME = "dispatch-coordinator-ssh-ed25519-v1"
LEGACY_COORDINATOR_SIGNATURE_NAMESPACE = "intelflo-dispatch-coordinator"
LEGACY_COORDINATOR_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIKw0FlrqA1ha584PV/saNa70na108hSaJmrNZEFUMvvG"
)
COORDINATOR_KEY_FILENAME = "coordinator-ed25519.pem"
COORDINATOR_LOCK_FILENAME = ".coordinator-key.lock"
MAX_COORDINATOR_KEY_BYTES = 4096
AUTHORITY_PROVIDER_TIMEOUT_S = 10.0
PROOF_FIELD = "terminal_authority_proof"
PTRACE_SCOPE_PATH = Path("/proc/sys/kernel/yama/ptrace_scope")
AuthorityKind = Literal["dispatcher", "coordinator"]


class TerminalAuthorityError(RuntimeError):
    """A terminal record has no valid dispatcher/coordinator authority proof."""


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
        raise TerminalAuthorityError("Ed25519 authority operation timed out") from exc
    if completed.returncode != 0:
        raise TerminalAuthorityError("Ed25519 authority operation failed")
    return completed.stdout


def _with_read_descriptor(payload: bytes, operation):
    if not hasattr(os, "memfd_create"):
        with tempfile.TemporaryFile(prefix="intelflo-dispatch-authority-") as handle:
            handle.write(payload)
            handle.flush()
            handle.seek(0)
            return operation(handle.fileno())
    read_fd = os.memfd_create("intelflo-dispatch-authority", os.MFD_CLOEXEC)
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
    return account_home / ".local" / "state" / "intelflo" / "dispatch-authority"


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


def _read_private_key(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o777 != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > MAX_COORDINATOR_KEY_BYTES
        ):
            raise TerminalAuthorityError("coordinator private key is unsafe")
        private_key = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TerminalAuthorityError("coordinator private key is unavailable") from exc
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
        if b"PRIVATE KEY" not in private_key or len(private_key) > MAX_COORDINATOR_KEY_BYTES:
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


def _trusted_coordinator_public_key() -> bytes:
    _require_process_memory_isolation()
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
    with tempfile.TemporaryDirectory(prefix="intelflo-coordinator-verify-") as raw:
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
            raise TerminalAuthorityError(
                "legacy coordinator verification timed out"
            ) from exc
        if completed.returncode == 0:
            return True
        if completed.returncode == 1 or (
            completed.returncode == 255
            and completed.stderr.startswith(b"Signature verification failed:")
        ):
            return False
        raise TerminalAuthorityError(
            "legacy coordinator verification provider failed"
        )


def verify_legacy_coordinator_authority(record: Mapping[str, object]) -> None:
    """Verify one retired SSH coordinator proof for cutover adoption only."""
    proof = record.get(PROOF_FIELD)
    if (
        not isinstance(proof, dict)
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
                    raise TerminalAuthorityError(
                        "terminal authority provider timed out"
                    ) from exc
                if completed.returncode == 0:
                    return True
                if completed.returncode == 1:
                    return False
                # Only OpenSSL's documented verification-mismatch result is
                # durable invalidity. Provider/runtime failures must remain
                # retryable and therefore must not enter the LRU cache.
                raise TerminalAuthorityError(
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
        public_key = _decode(
            proof.get("public_key"), label="public key", maximum=2048
        )
        try:
            trusted_public_key = _trusted_coordinator_public_key()
        except TerminalAuthorityError as exc:
            raise TerminalAuthorityError(
                "coordinator authority key is unavailable"
            ) from exc
        if public_key != trusted_public_key or proof.get(
            "key_id"
        ) != _coordinator_key_id(trusted_public_key):
            raise TerminalAuthorityError("coordinator authority key is invalid")
        signature = _decode(
            proof.get("signature"), label="signature", maximum=8192
        )
        payload = _canonical_payload(
            record,
            authority_kind="coordinator",
            scheme=COORDINATOR_AUTHORITY_SCHEME,
        )
        if not _signature_is_valid(public_key, signature, payload):
            raise TerminalAuthorityError(
                "coordinator authority signature is invalid"
            )
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
