#!/usr/bin/env python3
"""Ephemeral Ed25519 authority for dispatcher-authored terminal records."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
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
COORDINATOR_AUTHORITY_SCHEME = "dispatch-coordinator-ssh-ed25519-v1"
COORDINATOR_SIGNATURE_NAMESPACE = "intelflo-dispatch-coordinator"
COORDINATOR_SSH_TIMEOUT_S = 10.0
# Owner-controlled SSH-agent key. The public half is a trust anchor, not a
# secret; dispatched workers receive neither SSH_AUTH_SOCK nor the private key.
COORDINATOR_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIKw0FlrqA1ha584PV/saNa70na108hSaJmrNZEFUMvvG"
)
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
    completed = subprocess.run(
        [str(_openssl()), *arguments],
        input=input_bytes,
        capture_output=True,
        check=False,
        close_fds=True,
        pass_fds=inherited,
        env={"LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
    )
    if completed.returncode != 0:
        raise TerminalAuthorityError("Ed25519 authority operation failed")
    return completed.stdout


def _with_read_descriptor(payload: bytes, operation):
    if not hasattr(os, "memfd_create"):
        raise TerminalAuthorityError("sealed in-memory authority is unavailable")
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


def _coordinator_key_id(public_key: str) -> str:
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
    def generate(cls) -> "TerminalAuthority":
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
    """Host-anchored coordinator signer backed by the protected SSH agent."""

    public_key: str = COORDINATOR_PUBLIC_KEY

    @classmethod
    def from_ssh_agent(cls) -> "CoordinatorAuthority":
        socket_path = os.environ.get("SSH_AUTH_SOCK")
        if not socket_path:
            raise TerminalAuthorityError("coordinator SSH agent is unavailable")
        return cls()

    def seal(
        self,
        record: Mapping[str, object],
        *,
        authority_kind: AuthorityKind,
        include_public_key: bool = False,
    ) -> dict[str, object]:
        if authority_kind != "coordinator":
            raise TerminalAuthorityError(
                "SSH coordinator authority cannot seal dispatcher records"
            )
        if PROOF_FIELD in record:
            raise TerminalAuthorityError("terminal authority proof already exists")
        payload = _canonical_payload(
            record,
            authority_kind="coordinator",
            scheme=COORDINATOR_AUTHORITY_SCHEME,
        )
        signature = _ssh_sign(payload, self.public_key)
        return {
            **record,
            PROOF_FIELD: {
                "scheme": COORDINATOR_AUTHORITY_SCHEME,
                "authority_kind": "coordinator",
                "key_id": _coordinator_key_id(self.public_key),
                "public_key": self.public_key,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
        }


def _ssh_environment(*, require_agent: bool) -> dict[str, str]:
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": os.defpath}
    socket_path = os.environ.get("SSH_AUTH_SOCK")
    if require_agent:
        if not socket_path:
            raise TerminalAuthorityError("coordinator SSH agent is unavailable")
        environment["SSH_AUTH_SOCK"] = socket_path
    return environment


def _ssh_keygen() -> Path:
    try:
        return system_executable("ssh-keygen")
    except TrustedExecutableError as exc:
        raise TerminalAuthorityError("trusted SSH signer is unavailable") from exc


def _ssh_sign(payload: bytes, public_key: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="intelflo-coordinator-sign-") as raw:
        directory = Path(raw)
        key_path = directory / "key.pub"
        payload_path = directory / "payload"
        key_path.write_text(public_key + "\n", encoding="ascii")
        payload_path.write_bytes(payload)
        try:
            completed = subprocess.run(
                [
                    str(_ssh_keygen()),
                    "-Y",
                    "sign",
                    "-q",
                    "-f",
                    str(key_path),
                    "-n",
                    COORDINATOR_SIGNATURE_NAMESPACE,
                    str(payload_path),
                ],
                capture_output=True,
                check=False,
                close_fds=True,
                env=_ssh_environment(require_agent=True),
                timeout=COORDINATOR_SSH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise TerminalAuthorityError("coordinator SSH signing timed out") from exc
        signature_path = payload_path.with_suffix(".sig")
        try:
            signature = signature_path.read_bytes()
        except OSError as exc:
            raise TerminalAuthorityError(
                "coordinator SSH signing failed"
            ) from exc
        if completed.returncode != 0 or not signature or len(signature) > 4096:
            raise TerminalAuthorityError("coordinator SSH signing failed")
        return signature


@lru_cache(maxsize=4096)
def _coordinator_signature_is_valid(signature: bytes, payload: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="intelflo-coordinator-verify-") as raw:
        directory = Path(raw)
        allowed_path = directory / "allowed-signers"
        signature_path = directory / "signature"
        allowed_path.write_text(
            f"coordinator {COORDINATOR_PUBLIC_KEY}\n", encoding="ascii"
        )
        signature_path.write_bytes(signature)
        try:
            completed = subprocess.run(
                [
                    str(_ssh_keygen()),
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed_path),
                    "-I",
                    "coordinator",
                    "-n",
                    COORDINATOR_SIGNATURE_NAMESPACE,
                    "-s",
                    str(signature_path),
                ],
                input=payload,
                capture_output=True,
                check=False,
                close_fds=True,
                env=_ssh_environment(require_agent=False),
                timeout=COORDINATOR_SSH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return False
        return completed.returncode == 0


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
                )
                return completed.returncode == 0

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
        public_key = proof.get("public_key")
        if public_key != COORDINATOR_PUBLIC_KEY or proof.get(
            "key_id"
        ) != _coordinator_key_id(COORDINATOR_PUBLIC_KEY):
            raise TerminalAuthorityError("coordinator authority key is invalid")
        signature = _decode(
            proof.get("signature"), label="signature", maximum=8192
        )
        payload = _canonical_payload(
            record,
            authority_kind="coordinator",
            scheme=COORDINATOR_AUTHORITY_SCHEME,
        )
        if not _coordinator_signature_is_valid(signature, payload):
            raise TerminalAuthorityError(
                "coordinator authority signature is invalid"
            )
        return
    if (
        not isinstance(proof_scheme, str)
        or proof_scheme not in SUPPORTED_AUTHORITY_SCHEMES
    ):
        raise TerminalAuthorityError("terminal authority scheme is invalid")
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
