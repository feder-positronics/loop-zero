"""Run-scoped finding ownership before a delivery has a real PR.

The authority ledger owns admissions and one-time publication bindings.  This
module owns only immutable provisional finding content and deterministic replay.
Ordinary Finding Ledger records remain positively PR-scoped.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..config import Profile
from ..kernel.authority_projection import authenticated_coordinator_record_ids
from .authority import authenticated_review_terminals

ADMISSION_TYPE = "finding-recovery-admission-v1"
BINDING_TYPE = "finding-publication-binding-v1"
CAPTURE_TYPE = "provisional-finding-capture-v1"
FINDING_TYPE = "provisional-finding-v1"

_CONFIGURED_ROOT: Path | None = None
_PROVISIONAL_DIR = Path(".audit/provisional-findings")
_IDENTITY_FIELDS = (
    "task_id",
    "work_unit_id",
    "root_work_unit_id",
    "attempt_index",
    "unit_attempt_number",
    "run_id",
    "worktree",
    "read_only",
    "work_kind",
    "review_intent",
    "review_lens",
    "category",
    "source_identity",
    "output_identity",
    "baseline",
    "lineage",
    "snapshot_sha",
    "snapshot_tree_sha",
    "delta_from_snapshot_sha",
    "delta_from_tree_sha",
    "patch_identity",
    "task_contract_hash",
    "result_artifact",
    "result_sha256",
    "reservation_id",
    "generation_id",
    "family",
    "repository_binding",
)


class ProvisionalFindingError(RuntimeError):
    """A provisional finding transition is missing or contradictory."""


def load_recovery_result(
    repo: Path | str, admission: Mapping[str, object]
) -> dict[str, object]:
    """Reload the exact terminal-authenticated result without following symlinks."""
    artifact = admission.get("result_artifact")
    if not isinstance(artifact, str) or not artifact:
        raise ProvisionalFindingError("recovery result artifact is invalid")
    relative = Path(artifact)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProvisionalFindingError("recovery result artifact escapes repository")
    root = _root(repo)
    from ..kernel.authority_store import authority_repository_binding

    if authority_repository_binding(root) != admission.get("repository_binding"):
        raise ProvisionalFindingError("recovery repository binding changed")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ProvisionalFindingError("recovery result artifact uses a symlink")
    try:
        payload = cursor.read_bytes()
    except OSError as exc:
        raise ProvisionalFindingError(
            "recovery result artifact is unavailable"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != admission.get("result_sha256"):
        raise ProvisionalFindingError("recovery result artifact digest changed")
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProvisionalFindingError(
            "recovery result artifact is invalid JSON"
        ) from exc
    if not isinstance(result, dict):
        raise ProvisionalFindingError("recovery result artifact is not an object")
    return result


def configure(profile: Profile) -> None:
    """Bind the consumer-owned provisional stream path."""
    global _CONFIGURED_ROOT, _PROVISIONAL_DIR
    _CONFIGURED_ROOT = Path(profile.root).expanduser().resolve()
    _PROVISIONAL_DIR = Path(profile.audit_root) / "provisional-findings"


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProvisionalFindingError(
            "provisional payload is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _root(repo: Path | str) -> Path:
    from .findings import primary_repo_root

    resolved = Path(repo).expanduser().resolve()
    if _CONFIGURED_ROOT is not None and resolved == _CONFIGURED_ROOT:
        return resolved
    return primary_repo_root(resolved, require_git_primary=True)


def _admission_payload(terminal: Mapping[str, object]) -> dict[str, object]:
    identity = {field: terminal.get(field) for field in _IDENTITY_FIELDS}
    owner_id = "pfo_" + _digest(identity)
    return {
        "type": ADMISSION_TYPE,
        "status": "admitted",
        "recovery_classification": "finding-deposition-only",
        "provisional_owner_id": owner_id,
        **identity,
    }


def _eligible_terminal(record: Mapping[str, object]) -> bool:
    return (
        record.get("type") == "attempt-terminal"
        and record.get("status") == "infrastructure-failure"
        and record.get("failure_class") == "finding-deposition-failed"
        and record.get("deposit_state") == "none"
        and record.get("read_only") is True
        and record.get("work_kind") == "review"
        and record.get("advisory") is not True
        and record.get("review_intent") == "delivery-code-review"
        and all(
            record.get(field) is not None
            for field in (
                "task_id",
                "work_unit_id",
                "attempt_index",
                "run_id",
                "worktree",
                "snapshot_sha",
                "snapshot_tree_sha",
                "result_artifact",
                "result_sha256",
                "reservation_id",
                "generation_id",
                "family",
                "repository_binding",
                "source_identity",
                "task_contract_hash",
            )
        )
    )


def build_recovery_admission(
    records: Sequence[dict[str, object]], *, task_id: str
) -> dict[str, object]:
    """Derive an admission solely from one authenticated closed failure."""
    terminal = authenticated_review_terminals(records).get(task_id)
    if terminal is None or not _eligible_terminal(terminal):
        raise ProvisionalFindingError(
            "finding recovery requires one exact authenticated deposition failure"
        )
    if any(
        row.get("type") in {ADMISSION_TYPE, "attempt-recovery"}
        and row.get("task_id") == task_id
        for row in records
    ):
        admissions = authenticated_recovery_admissions(records)
        existing = admissions.get(task_id)
        if existing is not None:
            return {
                key: value
                for key, value in existing.items()
                if key != "terminal_authority_proof"
            }
        raise ProvisionalFindingError("finding recovery already has conflicting state")
    return _admission_payload(terminal)


def authenticated_recovery_admissions(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Project coordinator-authenticated admissions against preceding terminals."""
    coordinator_ids = authenticated_coordinator_record_ids(records)
    preceding: list[dict[str, object]] = []
    admitted: dict[str, dict[str, object]] = {}
    conflicted: set[str] = set()
    for record in records:
        if record.get("type") != ADMISSION_TYPE:
            preceding.append(record)
            continue
        task_id = record.get("task_id")
        if not isinstance(task_id, str) or id(record) not in coordinator_ids:
            continue
        terminal = authenticated_review_terminals(preceding).get(task_id)
        expected = (
            _admission_payload(terminal)
            if terminal and _eligible_terminal(terminal)
            else None
        )
        actual = {
            key: value
            for key, value in record.items()
            if key != "terminal_authority_proof"
        }
        if expected is None or actual != expected:
            continue
        if task_id in admitted and admitted[task_id] != record:
            conflicted.add(task_id)
        else:
            admitted[task_id] = record
        preceding.append(record)
    for task_id in conflicted:
        admitted.pop(task_id, None)
    return admitted


def _stream_records(repo: Path | str) -> list[dict[str, object]]:
    directory = _root(repo) / _PROVISIONAL_DIR
    if not directory.exists():
        return []
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProvisionalFindingError(
                    f"invalid JSON in provisional stream {path}:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise ProvisionalFindingError("invalid provisional stream record")
            records.append(row)
    return records


def _capture_receipts(
    records: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    receipts: dict[str, dict[str, object]] = {}
    members: Counter[str] = Counter()
    for row in records:
        owner = row.get("provisional_owner_id")
        if not isinstance(owner, str):
            raise ProvisionalFindingError("provisional record has no owner")
        if row.get("type") == CAPTURE_TYPE:
            if owner in receipts:
                raise ProvisionalFindingError(
                    "provisional owner has duplicate receipts"
                )
            receipts[owner] = dict(row)
        elif row.get("type") == FINDING_TYPE:
            members[owner] += 1
        else:
            raise ProvisionalFindingError("unknown provisional stream record")
    for owner, count in members.items():
        receipt = receipts.get(owner)
        if receipt is None or receipt.get("finding_count") != count:
            raise ProvisionalFindingError("provisional capture has no complete receipt")
    return receipts


def load_provisional_findings(
    repo: Path | str, *, owner_id: str | None = None
) -> list[dict[str, object]]:
    """Load immutable material findings after validating capture completeness."""
    records = _stream_records(repo)
    receipts = _capture_receipts(records)
    if owner_id is not None and owner_id not in receipts:
        raise ProvisionalFindingError(
            "authenticated provisional owner has no complete capture receipt"
        )
    return [
        row
        for row in records
        if row.get("type") == FINDING_TYPE
        and (owner_id is None or row.get("provisional_owner_id") == owner_id)
    ]


def _append_atomic(repo: Path | str, rows: Sequence[Mapping[str, object]]) -> None:
    directory = _root(repo) / _PROVISIONAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise ProvisionalFindingError("provisional stream is truncated")
    payload = existing + b"".join(_canonical(row) + b"\n" for row in rows)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def capture_provisional_findings(
    repo: Path | str,
    *,
    authority_records: Sequence[dict[str, object]],
    admission: Mapping[str, object],
    result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Persist one exact material-finding batch for an authenticated owner."""
    from .findings import ledger_lock

    task_id = admission.get("task_id")
    authenticated = authenticated_recovery_admissions(authority_records).get(
        str(task_id)
    )
    if authenticated != admission:
        raise ProvisionalFindingError("provisional owner is not authenticated")
    artifact_result = load_recovery_result(repo, admission)
    if result is not None and dict(result) != artifact_result:
        raise ProvisionalFindingError(
            "caller result does not match the authenticated artifact payload"
        )
    result = artifact_result
    owner = str(admission["provisional_owner_id"])
    raw_findings = result.get("findings")
    if not isinstance(raw_findings, list):
        raise ProvisionalFindingError("recovered result findings are invalid")
    material = []
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            raise ProvisionalFindingError("recovered result finding is invalid")
        if finding.get("severity") not in {"critical", "important", "suggestion"}:
            raise ProvisionalFindingError(
                "recovered result finding severity is invalid"
            )
        if not isinstance(finding.get("claim"), str) or not finding["claim"]:
            raise ProvisionalFindingError("recovered result finding claim is invalid")
        if finding.get("severity") != "suggestion":
            material.append(
                {
                    key: finding.get(key)
                    for key in ("severity", "claim", "path", "line_start", "line_end")
                }
            )
    request = {
        "provisional_owner_id": owner,
        "task_id": task_id,
        "attempt_index": admission.get("attempt_index"),
        "run_id": admission.get("run_id"),
        "result_sha256": admission.get("result_sha256"),
        "snapshot_sha": admission.get("snapshot_sha"),
        "snapshot_tree_sha": admission.get("snapshot_tree_sha"),
        "findings": material,
    }
    request_digest = _digest(request)
    with ledger_lock(repo) as primary:
        existing_records = _stream_records(primary)
        receipts = _capture_receipts(existing_records)
        existing = receipts.get(owner)
        if existing is not None:
            if existing.get("capture_request_sha256") != request_digest:
                raise ProvisionalFindingError(
                    "provisional owner was used with a different payload"
                )
            return existing
        rows = [
            {
                "type": FINDING_TYPE,
                "provisional_owner_id": owner,
                "finding_id": "pf_"
                + _digest({"owner": owner, "index": index, **finding}),
                "state": "open",
                "review_task_id": task_id,
                "delivery_run_id": admission.get("run_id"),
                **finding,
            }
            for index, finding in enumerate(material, 1)
        ]
        receipt = {
            "type": CAPTURE_TYPE,
            "provisional_owner_id": owner,
            "capture_request_sha256": request_digest,
            "finding_count": len(rows),
            "finding_ids": [row["finding_id"] for row in rows],
            "result_sha256": admission.get("result_sha256"),
        }
        _append_atomic(primary, [*rows, receipt])
        return receipt


def build_recovery_evidence(
    records: Sequence[dict[str, object]],
    *,
    repo: Path | str,
    admission: Mapping[str, object],
    capture_receipt: Mapping[str, object],
) -> dict[str, object]:
    """Build a no-launch recovery terminal from authenticated persisted facts."""
    task_id = str(admission.get("task_id") or "")
    if authenticated_recovery_admissions(records).get(task_id) != admission:
        raise ProvisionalFindingError("recovery admission is not authenticated")
    load_recovery_result(repo, admission)
    owner = admission.get("provisional_owner_id")
    if (
        capture_receipt.get("type") != CAPTURE_TYPE
        or capture_receipt.get("provisional_owner_id") != owner
        or capture_receipt.get("result_sha256") != admission.get("result_sha256")
        or not isinstance(capture_receipt.get("capture_request_sha256"), str)
    ):
        raise ProvisionalFindingError("recovery capture receipt is invalid")
    terminals = authenticated_review_terminals(records)
    terminal = terminals.get(task_id)
    if terminal is None or not _eligible_terminal(terminal):
        raise ProvisionalFindingError("recovery deposit is no longer authenticated")
    preserved = {
        key: value
        for key, value in terminal.items()
        if key
        not in {
            "type",
            "status",
            "failure_class",
            "deposit_state",
            "terminal_authority_proof",
            "ts",
        }
    }
    return {
        **preserved,
        "type": "attempt-recovery",
        "status": "completed",
        "recovered_terminal_status": terminal.get("status"),
        "recovery_classification": "finding-deposition-only",
        "provisional_owner_id": owner,
        "finding_capture_receipt_sha256": _digest(capture_receipt),
        "recovered_result_artifact": terminal.get("result_artifact"),
        "recovered_result_sha256": terminal.get("result_sha256"),
    }


def build_publication_binding(
    records: Sequence[dict[str, object]],
    *,
    repo: Path | str,
    admission: Mapping[str, object],
    capture_receipt: Mapping[str, object],
    pr: int,
    head: str,
    base: str,
    repository: str,
) -> dict[str, object]:
    """Build the permanent one-PR binding after publisher source revalidation."""
    if type(pr) is not int or pr <= 0:
        raise ProvisionalFindingError("publication binding requires a positive PR")
    if len(head) != 40 or not base.strip() or not repository.strip():
        raise ProvisionalFindingError("publication binding source identity is invalid")
    task_id = str(admission.get("task_id") or "")
    if authenticated_recovery_admissions(records).get(task_id) != admission:
        raise ProvisionalFindingError("publication binding owner is not authenticated")
    from ..kernel.authority_store import authority_repository_binding

    if authority_repository_binding(_root(repo)) != admission.get("repository_binding"):
        raise ProvisionalFindingError("publication repository binding changed")
    owner = str(admission.get("provisional_owner_id") or "")
    if (
        capture_receipt.get("type") != CAPTURE_TYPE
        or capture_receipt.get("provisional_owner_id") != owner
        or capture_receipt.get("result_sha256") != admission.get("result_sha256")
    ):
        raise ProvisionalFindingError("publication binding capture receipt is invalid")
    existing = authenticated_publication_bindings(records).get(owner)
    payload = {
        "type": BINDING_TYPE,
        "status": "bound",
        "provisional_owner_id": owner,
        "run_id": admission.get("run_id"),
        "task_id": task_id,
        "capture_receipt_sha256": _digest(capture_receipt),
        "pr": pr,
        "head": head,
        "base": base,
        "repository": repository,
        "repository_binding": admission.get("repository_binding"),
    }
    if existing is not None:
        if existing.get("pr") != pr:
            raise ProvisionalFindingError(
                "provisional owner is bound to a different PR"
            )
        unsigned = {
            key: value
            for key, value in existing.items()
            if key != "terminal_authority_proof"
        }
        if unsigned != payload:
            raise ProvisionalFindingError(
                "provisional binding replay changed source identity"
            )
        return unsigned
    return payload


def authenticated_publication_bindings(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Project one coordinator-authenticated immutable binding per owner."""
    coordinator_ids = authenticated_coordinator_record_ids(records)
    admissions = authenticated_recovery_admissions(records)
    owners = {row["provisional_owner_id"]: row for row in admissions.values()}
    result: dict[str, dict[str, object]] = {}
    conflicts: set[str] = set()
    for row in records:
        owner = row.get("provisional_owner_id")
        if (
            row.get("type") != BINDING_TYPE
            or not isinstance(owner, str)
            or owner not in owners
            or id(row) not in coordinator_ids
            or row.get("status") != "bound"
            or row.get("run_id") != owners[owner].get("run_id")
            or row.get("task_id") != owners[owner].get("task_id")
            or row.get("repository_binding") != owners[owner].get("repository_binding")
            or type(row.get("pr")) is not int
            or int(row["pr"]) <= 0
            or not isinstance(row.get("capture_receipt_sha256"), str)
            or len(str(row["capture_receipt_sha256"])) != 64
            or not isinstance(row.get("head"), str)
            or len(str(row["head"])) != 40
            or not isinstance(row.get("base"), str)
            or not str(row["base"]).strip()
            or not isinstance(row.get("repository"), str)
            or not str(row["repository"]).strip()
        ):
            continue
        if owner in result and result[owner] != row:
            conflicts.add(owner)
        else:
            result[owner] = row
    for owner in conflicts:
        result.pop(owner, None)
    return result


def materialize_publication_binding(
    repo: Path | str,
    *,
    authority_records: Sequence[dict[str, object]],
    binding: Mapping[str, object],
) -> dict[str, object]:
    """Project one bound provisional capture into the positive-PR ledger."""
    from .findings import build_finding_capture_request, capture_finding_records

    owner = str(binding.get("provisional_owner_id") or "")
    if authenticated_publication_bindings(authority_records).get(owner) != binding:
        raise ProvisionalFindingError("publication binding is not authenticated")
    from ..kernel.authority_store import authority_repository_binding

    if authority_repository_binding(_root(repo)) != binding.get("repository_binding"):
        raise ProvisionalFindingError("materialization repository binding changed")
    stream = _stream_records(repo)
    receipt = _capture_receipts(stream).get(owner)
    if receipt is None or _digest(receipt) != binding.get("capture_receipt_sha256"):
        raise ProvisionalFindingError(
            "publication binding has no exact capture receipt"
        )
    findings = load_provisional_findings(repo, owner_id=owner)
    request = build_finding_capture_request(
        pr=int(binding["pr"]),
        producer_id=str(binding["task_id"]),
        producer_skill="review",
        category="code",
        findings=findings,
    )
    candidates = [
        {
            **finding,
            "pr": binding["pr"],
            "finding_id": "f_"
            + _digest(
                {
                    "pr": binding["pr"],
                    "producer_id": binding["task_id"],
                    "provisional_finding_id": finding["finding_id"],
                }
            )[:24],
            "review_task_id": binding["task_id"],
            "delivery_run_id": binding["run_id"],
            "state": "open",
            "status": "open",
            "disposition": None,
            "advisory": False,
        }
        for finding in findings
    ]
    return capture_finding_records(
        repo,
        request=request,
        candidate_records=candidates,
        source_identity={"head": binding["head"], "base": binding["base"]},
    )


__all__ = [
    "ADMISSION_TYPE",
    "BINDING_TYPE",
    "CAPTURE_TYPE",
    "FINDING_TYPE",
    "ProvisionalFindingError",
    "authenticated_publication_bindings",
    "authenticated_recovery_admissions",
    "build_publication_binding",
    "build_recovery_admission",
    "build_recovery_evidence",
    "capture_provisional_findings",
    "configure",
    "load_provisional_findings",
    "load_recovery_result",
    "materialize_publication_binding",
]
