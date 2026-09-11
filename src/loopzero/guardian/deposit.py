#!/usr/bin/env python3
"""Trusted promotion boundary for one untrusted Guardian dispatch."""

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..kernel import settings as kernel_settings

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
MAX_RECORD_BYTES = 256 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_DEPOSIT_APPEND_BYTES = 16 * 1024 * 1024
MAX_DEPOSIT_RECORDS = 1024
MAX_DEPOSIT_PATHS = 16 * 1024
MAX_DEPOSIT_DEPTH = 8
TRIMMABLE_RECORD_TYPES = frozenset({"attempt-checkpoint", "attempt-progress"})
PROMOTABLE_RECORD_TYPES = frozenset(
    {
        "attempt-checkpoint",
        "attempt-owner",
        "attempt-progress",
        "attempt-start",
        "attempt-terminal",
    }
)


class AuditDepositError(RuntimeError):
    """An untrusted dispatch deposit could not be promoted safely."""


@dataclass(frozen=True)
class AuditDeposit:
    task_id: str
    root: Path
    audit_root: Path
    canonical_audit_root: Path
    manifest_path: Path

    @property
    def result_path(self) -> Path:
        return self.audit_root / "dispatch" / "results" / f"{self.task_id}.json"

    @property
    def canonical_result_path(self) -> Path:
        return (
            self.canonical_audit_root / "dispatch" / "results" / f"{self.task_id}.json"
        )

    @property
    def authority_projection_root(self) -> Path | None:
        path = self.root / "authority-projection"
        return path if path.is_dir() else None


def _copy_dispatch_history(*, repo: Path, source: Path, destination: Path) -> None:
    from ..kernel.authority_projection import coordinator_ledger_prefix
    from ..kernel.authority_store import create_coordinator_authority, load_authority_records
    from ..kernel.gitscope import DispatchError
    from ..kernel.policy import (
        DISPATCH_POLICY_VERSION,
        RUNTIME_CONTRACT_VERSION,
        TELEMETRY_SCHEMA_VERSION,
    )

    try:
        records = load_authority_records(repo, 30)
    except DispatchError as exc:
        raise AuditDepositError("canonical dispatch authority is unavailable") from exc
    if not (source / "ledger").is_dir():
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.mkdir(parents=True, mode=0o700)
        return

    def ignore_checkpoint_authority(path: str, names: list[str]) -> set[str]:
        if Path(path) != source:
            return set()
        return {
            name
            for name in names
            if name in {"ledger", "archives"} or name.endswith(".jsonl")
        }

    shutil.copytree(source, destination, ignore=ignore_checkpoint_authority)
    cutover = {
        "type": "coordinator-authority-cutover",
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "policy_version": DISPATCH_POLICY_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "status": "active",
        "ledger_prefix": coordinator_ledger_prefix(records),
    }
    signed_cutover = create_coordinator_authority().seal(
        cutover,
        authority_kind="coordinator",
        include_public_key=True,
    )
    proof = signed_cutover.get("terminal_authority_proof")
    if not isinstance(proof, dict) or not isinstance(proof.get("public_key"), str):
        raise AuditDepositError("coordinator authority projection is invalid")
    try:
        public_key = base64.b64decode(proof["public_key"], validate=True)
    except (ValueError, TypeError) as exc:
        raise AuditDepositError("coordinator authority projection is invalid") from exc
    authority_projection = destination.parent.parent / "authority-projection"
    authority_projection.mkdir(mode=0o700)
    public_key_path = authority_projection / "coordinator-public-key.der"
    public_key_path.write_bytes(public_key)
    public_key_path.chmod(0o600)
    projection = destination / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    projection.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in (*records, signed_cutover)
        ),
        encoding="utf-8",
    )
    projection.chmod(0o600)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(
    *, repo: Path, state_dir: Path, task_id: str, reset: bool = False,
    audit_root: Path | None = None,
) -> AuditDeposit:
    """Create a private copy of dispatch history, never canonical observations."""
    if TASK_ID_RE.fullmatch(task_id) is None:
        raise AuditDepositError("dispatch deposit task identity is malformed")
    configured_audit = audit_root or kernel_settings.settings.audit_root
    if configured_audit.is_absolute():
        canonical_audit = configured_audit.resolve()
    else:
        canonical_audit = repo.resolve() / configured_audit
    root = state_dir.resolve() / "dispatch-deposits" / task_id
    audit_root = root / "audit"
    manifest_path = root / "manifest.json"
    if reset and root.exists():
        shutil.rmtree(root)
    if manifest_path.is_file() and audit_root.is_dir():
        return AuditDeposit(task_id, root, audit_root, canonical_audit, manifest_path)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, mode=0o700)
    source = canonical_audit / "dispatch"
    destination = audit_root / "dispatch"
    _copy_dispatch_history(repo=repo.resolve(), source=source, destination=destination)
    baseline = {
        path.relative_to(destination).as_posix(): {
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in destination.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": task_id,
                "created_at": datetime.now(UTC).isoformat(),
                "baseline": baseline,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return AuditDeposit(task_id, root, audit_root, canonical_audit, manifest_path)


def _load_manifest(deposit: AuditDeposit) -> dict[str, object]:
    try:
        payload = json.loads(deposit.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditDepositError("dispatch deposit manifest is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("task_id") != deposit.task_id
        or not isinstance(payload.get("baseline"), dict)
    ):
        raise AuditDepositError("dispatch deposit manifest is malformed")
    return payload


def bind_launch(
    deposit: AuditDeposit,
    *,
    task: dict[str, object],
    run_id: str,
    worktree: Path,
    read_only: bool,
) -> None:
    """Bind a private deposit to the exact trusted-parent launch contract."""
    manifest = _load_manifest(deposit)
    if task.get("task_id") != deposit.task_id or not run_id:
        raise AuditDepositError("dispatch deposit launch binding is invalid")
    binding = {
        "task": task,
        "run_id": run_id,
        "worktree": str(worktree.resolve()),
        "read_only": read_only,
    }
    existing = manifest.get("launch")
    if existing is not None and existing != binding:
        raise AuditDepositError("dispatch deposit launch binding conflicts")
    if existing == binding:
        return
    manifest["launch"] = binding
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=deposit.root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).chmod(0o600)
        Path(temporary).replace(deposit.manifest_path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def _launch_binding(deposit: AuditDeposit) -> dict[str, object]:
    launch = _load_manifest(deposit).get("launch")
    if (
        not isinstance(launch, dict)
        or not isinstance(launch.get("task"), dict)
        or launch["task"].get("task_id") != deposit.task_id
        or not isinstance(launch.get("run_id"), str)
        or not launch["run_id"]
        or not isinstance(launch.get("worktree"), str)
        or not isinstance(launch.get("read_only"), bool)
    ):
        raise AuditDepositError("dispatch deposit launch binding is invalid")
    return launch


def _open_deposit_authority(root: Path, relative: str) -> tuple[int, os.stat_result]:
    parts = Path(relative).parts
    if (
        Path(relative).is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise AuditDepositError("dispatch deposit authority is unsafe")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent = os.open(root, directory_flags)
    except OSError as exc:
        raise AuditDepositError("dispatch deposit authority is unsafe") from exc
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        initial = os.stat(  # noqa: PTH116 -- dir_fd preserves the no-symlink walk
            parts[-1], dir_fd=parent, follow_symlinks=False
        )
        if not stat.S_ISREG(initial.st_mode):
            raise AuditDepositError("dispatch deposit authority is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(parts[-1], flags, dir_fd=parent)
    except (OSError, AuditDepositError) as exc:
        if isinstance(exc, AuditDepositError):
            raise
        raise AuditDepositError("dispatch deposit authority is unsafe") from exc
    finally:
        os.close(parent)
    return descriptor, initial


def _read_deposit_authority(root: Path, relative: str, *, max_bytes: int) -> bytes:
    descriptor, initial = _open_deposit_authority(root, relative)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            initial.st_dev,
            initial.st_ino,
        ):
            raise AuditDepositError("dispatch deposit authority is unsafe")
        if opened.st_size > max_bytes:
            raise AuditDepositError("dispatch deposit authority exceeds size budget")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise AuditDepositError("dispatch deposit authority exceeds size budget")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) or len(payload) != opened.st_size:
            raise AuditDepositError("dispatch deposit authority changed while reading")
        return payload
    finally:
        os.close(descriptor)


def _bounded_deposit_jsonl_paths(root: Path) -> list[str]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise AuditDepositError("dispatch deposit authority is unsafe") from exc
    paths: list[str] = []
    path_count = 0

    def walk(descriptor: int, prefix: tuple[str, ...], depth: int) -> None:
        nonlocal path_count
        try:
            entries = os.scandir(descriptor)
            with entries:
                for entry in entries:
                    path_count += 1
                    if path_count > MAX_DEPOSIT_PATHS:
                        raise AuditDepositError(
                            "dispatch deposit exceeds path count budget"
                        )
                    relative_parts = (*prefix, entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        if depth >= MAX_DEPOSIT_DEPTH:
                            raise AuditDepositError(
                                "dispatch deposit exceeds depth budget"
                            )
                        child = os.open(entry.name, directory_flags, dir_fd=descriptor)
                        try:
                            walk(child, relative_parts, depth + 1)
                        finally:
                            os.close(child)
                    elif entry.name.endswith(".jsonl"):
                        paths.append("/".join(relative_parts))
        except AuditDepositError:
            raise
        except OSError as exc:
            raise AuditDepositError("dispatch deposit authority is unsafe") from exc

    try:
        walk(root_descriptor, (), 0)
    finally:
        os.close(root_descriptor)
    return sorted(paths)


def _deposited_records(deposit: AuditDeposit) -> list[dict[str, object]]:
    manifest = _load_manifest(deposit)
    baseline = manifest["baseline"]
    assert isinstance(baseline, dict)
    dispatch = deposit.audit_root / "dispatch"
    records: list[dict[str, object]] = []
    baseline_sizes: dict[str, int] = {}
    baseline_jsonl_bytes = 0
    for relative, original in baseline.items():
        if (
            not isinstance(original, dict)
            or not isinstance(original.get("size"), int)
            or isinstance(original.get("size"), bool)
            or int(original["size"]) < 0
            or not isinstance(original.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(original["sha256"])) is None
        ):
            raise AuditDepositError("dispatch deposit baseline is malformed")
        baseline_sizes[relative] = int(original["size"])
        if relative.endswith(".jsonl"):
            baseline_jsonl_bytes += int(original["size"])
    remaining_bytes = baseline_jsonl_bytes + MAX_DEPOSIT_APPEND_BYTES
    record_limit = MAX_DEPOSIT_RECORDS
    seen_jsonl: set[str] = set()
    for relative in _bounded_deposit_jsonl_paths(dispatch):
        original = baseline.get(relative)
        payload = _read_deposit_authority(dispatch, relative, max_bytes=remaining_bytes)
        remaining_bytes -= len(payload)
        offset = 0
        if original is not None:
            seen_jsonl.add(relative)
            offset = baseline_sizes[relative]
            prefix = payload[:offset]
            if len(payload) < offset or hashlib.sha256(
                prefix
            ).hexdigest() != original.get("sha256"):
                raise AuditDepositError("dispatch deposit rewrote governed history")
        suffix = payload[offset:]
        for raw in suffix.splitlines():
            if not raw:
                continue
            if len(raw) > MAX_RECORD_BYTES:
                raise AuditDepositError("dispatch deposit record is oversized")
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AuditDepositError("dispatch deposit record is malformed") from exc
            if not isinstance(row, dict) or row.get("task_id") != deposit.task_id:
                raise AuditDepositError("dispatch deposit contains foreign authority")
            if row.get("type") not in PROMOTABLE_RECORD_TYPES:
                raise AuditDepositError(
                    "dispatch deposit contains unsupported authority family"
                )
            records.append(row)
    for relative, original in baseline.items():
        if relative.endswith(".jsonl"):
            if relative not in seen_jsonl:
                raise AuditDepositError("dispatch deposit rewrote governed history")
            continue
        payload = _read_deposit_authority(
            dispatch, relative, max_bytes=baseline_sizes[relative]
        )
        assert isinstance(original, dict)
        if hashlib.sha256(payload).hexdigest() != original.get("sha256"):
            raise AuditDepositError("dispatch deposit changed unrelated evidence")
    if len(records) > record_limit:
        required = {
            index
            for index, record in enumerate(records)
            if record.get("type") not in TRIMMABLE_RECORD_TYPES
        }
        if len(required) > record_limit:
            raise AuditDepositError(
                "dispatch deposit contains too many non-observational authority records"
            )
        remaining = record_limit - len(required)
        observational = [
            index
            for index, record in enumerate(records)
            if record.get("type") in TRIMMABLE_RECORD_TYPES
        ]
        if remaining:
            required.update(observational[-remaining:])
        records = [record for index, record in enumerate(records) if index in required]
    return records


def promote(
    deposit: AuditDeposit,
    *,
    append_authority_records: Callable[[Path, list[dict[str, object]]], None],
) -> Path:
    """Promote only task-bound telemetry and its bounded terminal result."""
    launch = _launch_binding(deposit)
    dispatch = deposit.audit_root / "dispatch"
    result_relative = f"results/{deposit.task_id}.json"
    payload = _read_deposit_authority(
        dispatch, result_relative, max_bytes=MAX_RESULT_BYTES
    )
    try:
        result_payload = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditDepositError("dispatch deposit result is malformed") from exc
    if (
        not isinstance(result_payload, dict)
        or result_payload.get("task_id") != deposit.task_id
    ):
        raise AuditDepositError("dispatch deposit result identity is invalid")
    records = _deposited_records(deposit)
    terminal = [
        row
        for row in records
        if row.get("type") == "attempt-terminal" and row.get("status") == "completed"
    ]
    if len(terminal) != 1:
        raise AuditDepositError("dispatch deposit terminal evidence is invalid")
    artifact = terminal[0].get("result_artifact")
    digest = terminal[0].get("result_sha256")
    if (
        artifact != (
            deposit.canonical_audit_root.relative_to(deposit.canonical_audit_root.parent).as_posix()
            + f"/dispatch/results/{deposit.task_id}.json"
        )
        or digest != hashlib.sha256(payload).hexdigest()
    ):
        raise AuditDepositError("dispatch deposit result provenance is invalid")

    from ..kernel.authority import TerminalAuthorityError
    from ..kernel.authority_projection import retained_attempt_settlements
    from ..kernel.authority_store import (
        authority_ledger_lock,
        create_coordinator_authority,
        load_authority_records,
    )
    from ..kernel.canonical import canonical_record_digest
    from ..kernel.gitscope import (
        DispatchError,
        immutable_task_contract,
        task_contract_hash,
    )
    from ..kernel.seams import authenticated_retry_outcomes

    repo = deposit.canonical_audit_root.parent
    starts = [row for row in records if row.get("type") == "attempt-start"]
    if not starts:
        raise AuditDepositError("dispatch deposit start evidence is invalid")
    expected_contract = immutable_task_contract(launch["task"])
    translated: list[dict[str, object]] = []
    try:
        coordinator = create_coordinator_authority()
        for row in records:
            if row.get("type") != "attempt-start":
                translated.append(row)
                continue
            actual_contract = row.get("task_contract")
            if (
                row.get("run_id") != launch["run_id"]
                or row.get("worktree") != launch["worktree"]
                or row.get("read_only") is not launch["read_only"]
                or not isinstance(actual_contract, dict)
                or any(
                    actual_contract.get(key) != value
                    for key, value in expected_contract.items()
                )
                or row.get("task_contract_hash") != task_contract_hash(actual_contract)
            ):
                raise AuditDepositError("dispatch deposit start binding is invalid")
            unsigned = dict(row)
            unsigned.pop("terminal_authority_proof", None)
            translated.append(coordinator.seal(unsigned, authority_kind="coordinator"))
    except (DispatchError, TerminalAuthorityError) as exc:
        raise AuditDepositError(
            "dispatch deposit start authority is unavailable"
        ) from exc

    target = deposit.canonical_result_path
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{deposit.task_id}-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        with authority_ledger_lock(repo):
            try:
                try:
                    target_state = target.lstat()
                except FileNotFoundError:
                    # A staged result is retry evidence, not terminal completion;
                    # authority import below must still succeed on this or a later call.
                    temporary_path.replace(target)
                else:
                    if not stat.S_ISREG(target_state.st_mode):
                        raise AuditDepositError(
                            "canonical dispatch result conflicts"
                        )
                    existing_payload = _read_deposit_authority(
                        target.parent, target.name, max_bytes=MAX_RESULT_BYTES
                    )
                    if existing_payload != payload:
                        raise AuditDepositError(
                            "canonical dispatch result conflicts"
                        )
                directory_descriptor = os.open(
                    target.parent,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except AuditDepositError:
                raise
            except OSError as exc:
                raise AuditDepositError(
                    "canonical dispatch result could not be published"
                ) from exc

            try:
                existing = load_authority_records(repo, 30)
                existing_rows = list(existing)
                existing_outcomes = authenticated_retry_outcomes(existing)
                retained_settlements = {
                    (
                        row["task_id"],
                        row["attempt_index"],
                        row["source_record_digest"],
                    )
                    for row in retained_attempt_settlements(existing)
                }
                retained_replay_attempts = {
                    (
                        row.get("task_id"),
                        row.get("attempt_index")
                        if row.get("attempt_index") is not None
                        else -1,
                    )
                    for row in translated
                    if row.get("type") == "attempt-terminal"
                    and (
                        row.get("task_id"),
                        row.get("attempt_index")
                        if row.get("attempt_index") is not None
                        else -1,
                        canonical_record_digest(row),
                    )
                    in retained_settlements
                }
                admitted_rows: list[dict[str, object]] = []
                novel_rows: list[dict[str, object]] = []
                for row in translated:
                    attempt = (
                        row.get("task_id"),
                        row.get("attempt_index")
                        if row.get("attempt_index") is not None
                        else -1,
                    )
                    prior = next(
                        (candidate for candidate in existing_rows if candidate == row),
                        None,
                    )
                    if (
                        prior is None
                        and row.get("type") == "attempt-terminal"
                        and attempt in retained_replay_attempts
                    ):
                        prior = next(
                            (
                                candidate
                                for candidate in existing_outcomes
                                if (
                                    candidate.get("task_id"),
                                    candidate.get("attempt_index")
                                    if candidate.get("attempt_index") is not None
                                    else -1,
                                )
                                == attempt
                            ),
                            None,
                        )
                    admitted_rows.append(prior if prior is not None else row)
                    if prior is None and attempt not in retained_replay_attempts:
                        novel_rows.append(row)
                existing.extend(novel_rows)
                accepted_outcomes = authenticated_retry_outcomes(existing)
                accepted_ids = {id(row) for row in accepted_outcomes}
                accepted_attempts = {
                    (row.get("task_id"), row.get("attempt_index"))
                    for row in accepted_outcomes
                }
            except DispatchError as exc:
                raise AuditDepositError(
                    "dispatch deposit authority is invalid"
                ) from exc
            settlement_rows = [
                row for row in admitted_rows if row.get("type") == "attempt-terminal"
            ]
            if not settlement_rows or any(
                id(row) not in accepted_ids
                and (row.get("task_id"), row.get("attempt_index"))
                not in accepted_attempts
                for row in settlement_rows
            ):
                raise AuditDepositError("dispatch deposit terminal authority is invalid")

            try:
                append_authority_records(repo, novel_rows)
            except DispatchError as exc:
                raise AuditDepositError(
                    "dispatch deposit authority could not be promoted"
                ) from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return target


def promote_completed(
    deposit: AuditDeposit,
    *,
    append_authority_records: Callable[[Path, list[dict[str, object]]], None],
) -> Path | None:
    """Retry promotion while private result evidence remains available."""
    if not deposit.result_path.is_file():
        return None
    return promote(deposit, append_authority_records=append_authority_records)


def disposable_git_metadata(
    *, repo: Path, deposit: AuditDeposit | None = None, destination: Path | None = None
) -> Path:
    """Copy common Git metadata so an untrusted review cannot mutate canonical Git."""
    source = repo.resolve() / ".git"
    if source.is_symlink() or not source.is_dir():
        raise AuditDepositError("canonical Git metadata is unavailable")
    if (deposit is None) == (destination is None):
        raise AuditDepositError("choose one disposable Git destination")
    target = deposit.root / "git-metadata" if deposit is not None else destination
    assert target is not None
    if target.exists():
        shutil.rmtree(target)

    def ignore_root_objects(path: str, names: list[str]) -> set[str]:
        return (
            {"objects"}
            if Path(path).resolve() == source and "objects" in names
            else set()
        )

    shutil.copytree(source, target, ignore=ignore_root_objects)
    alternates = target / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True)
    alternates.write_text("/run/guardian-source-objects\n", encoding="utf-8")
    return target
