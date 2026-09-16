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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..config import Profile
from ..kernel.authority_projection import (
    AuthorityRecordView,
    authenticated_coordinator_record_ids,
)
from ..kernel.gitscope import DispatchError
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
_ENVELOPE_FIELDS = frozenset({"ts", "schema_version", "policy_version"})


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
    configure_kernel_seams()


def configure_kernel_seams() -> None:
    """Bind finding authority projections to the package kernel seam."""
    from ..kernel import seams

    seams.configure(
        authenticated_publication_bindings=authenticated_publication_bindings,
        authenticated_recovery_admissions=authenticated_recovery_admissions,
        authenticated_capture_admissions=authenticated_capture_admissions,
    )


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


def _semantic_authority_payload(record: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"terminal_authority_proof", *_ENVELOPE_FIELDS}
    }


def _matches_enveloped_payload(
    record: Mapping[str, object], expected: Mapping[str, object]
) -> bool:
    from ..kernel.policy import DISPATCH_POLICY_VERSION, TELEMETRY_SCHEMA_VERSION

    semantic_expected = _semantic_authority_payload(expected)
    if _semantic_authority_payload(record) != semantic_expected:
        return False
    if (
        set(record)
        - set(semantic_expected)
        - _ENVELOPE_FIELDS
        - {"terminal_authority_proof"}
    ):
        return False
    present = set(record).intersection(_ENVELOPE_FIELDS)
    return (
        present == _ENVELOPE_FIELDS
        and isinstance(record.get("ts"), str)
        and bool(str(record.get("ts")))
        and record.get("schema_version") == TELEMETRY_SCHEMA_VERSION
        and record.get("policy_version") == DISPATCH_POLICY_VERSION
    )


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
            return _semantic_authority_payload(existing)
        raise ProvisionalFindingError("finding recovery already has conflicting state")
    return _admission_payload(terminal)


def authenticated_recovery_admissions(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Project coordinator-authenticated admissions against preceding terminals."""
    coordinator_ids = authenticated_coordinator_record_ids(records)
    preceding: list[dict[str, object]] = []
    admitted: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("type") != ADMISSION_TYPE:
            preceding.append(record)
            continue
        task_id = record.get("task_id")
        if not isinstance(task_id, str) or id(record) not in coordinator_ids:
            continue
        authenticated_prefix = (
            records.filtered(preceding)
            if isinstance(records, AuthorityRecordView)
            else preceding
        )
        terminal = authenticated_review_terminals(authenticated_prefix).get(task_id)
        expected = (
            _admission_payload(terminal)
            if terminal and _eligible_terminal(terminal)
            else None
        )
        if expected is None or not _matches_enveloped_payload(record, expected):
            continue
        admitted.setdefault(task_id, record)
        preceding.append(record)
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
    members: dict[str, list[str]] = {}
    member_counts: dict[str, int] = {}
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
            finding_id = row.get("finding_id")
            if not isinstance(finding_id, str) or not finding_id:
                raise ProvisionalFindingError("provisional finding has no identity")
            index = member_counts.get(owner, 0) + 1
            member_counts[owner] = index
            finding = {
                key: row.get(key)
                for key in (
                    "severity",
                    "claim",
                    "path",
                    "line_start",
                    "line_end",
                    "content_anchor",
                )
            }
            expected_id = "pf_" + _digest(
                {"owner": owner, "index": index, **finding}
            )
            expected_keys = {
                "type",
                "provisional_owner_id",
                "finding_id",
                "state",
                "review_task_id",
                "delivery_run_id",
                *finding,
            }
            if (
                set(row) != expected_keys
                or row.get("state") != "open"
                or finding_id != expected_id
            ):
                raise ProvisionalFindingError(
                    "provisional capture has no complete receipt: "
                    "finding content does not match its identity"
                )
            owned = members.setdefault(owner, [])
            if finding_id in owned:
                raise ProvisionalFindingError(
                    "provisional finding identity is duplicated"
                )
            owned.append(finding_id)
        else:
            raise ProvisionalFindingError("unknown provisional stream record")
    for owner in set(members) | set(receipts):
        finding_ids = members.get(owner, [])
        receipt = receipts.get(owner)
        if (
            receipt is None
            or receipt.get("finding_count") != len(finding_ids)
            or receipt.get("finding_ids") != finding_ids
        ):
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


def authenticated_provisional_findings(
    records: Sequence[dict[str, object]],
    repo: Path | str,
    *,
    admission: Mapping[str, object],
) -> list[dict[str, object]]:
    """Load findings only when a signed review terminal binds their exact receipt."""
    task_id = str(admission.get("task_id") or "")
    if authenticated_provisional_admissions(records).get(task_id) != admission:
        raise ProvisionalFindingError("provisional owner is not authenticated")
    owner = str(admission.get("provisional_owner_id") or "")
    persisted_receipt = _capture_receipts(_stream_records(repo)).get(owner)
    recovery = authenticated_review_terminals(records).get(task_id)
    if (
        persisted_receipt is None
        or recovery is None
        or (
            (
                admission.get("type") == ADMISSION_TYPE
                and (
                    recovery.get("type") != "attempt-recovery"
                    or recovery.get("recovery_classification")
                    != "finding-deposition-only"
                )
            )
            or (
                admission.get("type") != ADMISSION_TYPE
                and recovery.get("type") != "attempt-terminal"
            )
        )
        or recovery.get("provisional_owner_id") != owner
        or recovery.get("finding_capture_receipt_sha256") != _digest(persisted_receipt)
    ):
        raise ProvisionalFindingError(
            "provisional findings do not match the authenticated recovery"
        )
    return load_provisional_findings(repo, owner_id=owner)


def _git_output(repo: Path, *args: str) -> str:
    from .evidence import _snapshot_git

    try:
        return _snapshot_git(repo, *args)
    except DispatchError:
        raise ProvisionalFindingError(
            "authenticated predecessor snapshot is unavailable"
        ) from None


def _authenticated_snapshot(
    repo: Path, admission: Mapping[str, object]
) -> tuple[str, str]:
    commit = admission.get("snapshot_sha")
    tree = admission.get("snapshot_tree_sha")
    if not isinstance(commit, str) or not isinstance(tree, str):
        raise ProvisionalFindingError("authenticated predecessor snapshot is invalid")
    resolved_commit = _git_output(repo, "rev-parse", f"{commit}^{{commit}}")
    resolved_tree = _git_output(repo, "rev-parse", f"{commit}^{{tree}}")
    if resolved_commit != commit or resolved_tree != tree:
        raise ProvisionalFindingError("authenticated predecessor snapshot changed")
    return commit, tree


def _content_anchor(
    repo: Path, snapshot_sha: str, finding: Mapping[str, object]
) -> dict[str, object] | None:
    raw_path = finding.get("path")
    if raw_path is None:
        return None
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or Path(raw_path).is_absolute()
        or ".." in Path(raw_path).parts
        or ":" in raw_path
        or "\n" in raw_path
    ):
        raise ProvisionalFindingError("recovered finding path is invalid")
    blob = _git_output(repo, "rev-parse", f"{snapshot_sha}:{raw_path}")
    anchor: dict[str, object] = {"path": raw_path, "blob_sha": blob}
    start = finding.get("line_start")
    end = finding.get("line_end")
    if start is None and end is None:
        return anchor
    if (
        type(start) is not int
        or start < 1
        or (end is not None and (type(end) is not int or end < start))
    ):
        raise ProvisionalFindingError("recovered finding line span is invalid")
    span_end = start if end is None else end
    content = _git_output(repo, "show", f"{snapshot_sha}:{raw_path}")
    lines = content.splitlines()
    if span_end > len(lines):
        raise ProvisionalFindingError("recovered finding line span exceeds snapshot")
    anchor["line_span"] = [start, span_end]
    anchor["hunk_context_sha"] = hashlib.sha256(
        "\n".join(lines[max(0, start - 4) : span_end + 3]).encode()
    ).hexdigest()
    return anchor


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
    authenticated = authenticated_provisional_admissions(authority_records).get(
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
    if admission.get("type") != ADMISSION_TYPE:
        from .evidence import persisted_review_findings

        material = persisted_review_findings(
            material, review_intent=str(admission.get("review_intent"))
        )
    with ledger_lock(repo) as primary:
        snapshot_sha, snapshot_tree_sha = _authenticated_snapshot(primary, admission)
        anchored = [
            {
                **finding,
                "content_anchor": _content_anchor(primary, snapshot_sha, finding),
            }
            for finding in material
        ]
        # Re-resolve immediately before persistence so a caller cannot swap the
        # predecessor ref between validation and the immutable capture.
        if _authenticated_snapshot(primary, admission) != (
            snapshot_sha,
            snapshot_tree_sha,
        ):
            raise ProvisionalFindingError("authenticated predecessor snapshot changed")
        request = {
            "provisional_owner_id": owner,
            "task_id": task_id,
            "attempt_index": admission.get("attempt_index"),
            "run_id": admission.get("run_id"),
            "result_sha256": admission.get("result_sha256"),
            "snapshot_sha": snapshot_sha,
            "snapshot_tree_sha": snapshot_tree_sha,
            "findings": anchored,
        }
        request_digest = _digest(request)
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
            for index, finding in enumerate(anchored, 1)
        ]
        receipt = {
            "type": CAPTURE_TYPE,
            "provisional_owner_id": owner,
            "capture_request_sha256": request_digest,
            "finding_count": len(rows),
            "finding_ids": [row["finding_id"] for row in rows],
            "result_sha256": admission.get("result_sha256"),
        }
        existing_records = _stream_records(primary)
        receipts = _capture_receipts(existing_records)
        existing = receipts.get(owner)
        if existing is not None:
            existing_rows = [
                row
                for row in existing_records
                if row.get("type") == FINDING_TYPE
                and row.get("provisional_owner_id") == owner
            ]
            if existing != receipt or existing_rows != rows:
                raise ProvisionalFindingError(
                    "provisional owner was used with a different payload"
                )
            return existing
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
    result = load_recovery_result(repo, admission)
    owner = admission.get("provisional_owner_id")
    persisted_receipt = _capture_receipts(_stream_records(repo)).get(str(owner))
    if persisted_receipt is None or dict(capture_receipt) != persisted_receipt:
        raise ProvisionalFindingError(
            "recovery requires the exact persisted capture receipt"
        )
    validated_receipt = capture_provisional_findings(
        repo,
        authority_records=records,
        admission=admission,
        result=result,
    )
    if validated_receipt != persisted_receipt:
        raise ProvisionalFindingError(
            "recovery capture no longer matches the authenticated result"
        )
    if (
        capture_receipt.get("type") != CAPTURE_TYPE
        or capture_receipt.get("provisional_owner_id") != owner
        or capture_receipt.get("result_sha256") != admission.get("result_sha256")
        or not isinstance(capture_receipt.get("capture_request_sha256"), str)
    ):
        raise ProvisionalFindingError("recovery capture receipt is invalid")
    terminals = authenticated_review_terminals(records)
    terminal = terminals.get(task_id)
    if terminal is not None and terminal.get("type") == "attempt-recovery":
        existing = [
            (index, record)
            for index, record in enumerate(records)
            if record.get("type") == "attempt-recovery"
            and record.get("task_id") == task_id
        ]
        if (
            len(existing) != 1
            or existing[0][1] is not terminal
            or terminal.get("recovery_classification")
            != "finding-deposition-only"
            or terminal.get("provisional_owner_id") != owner
        ):
            raise ProvisionalFindingError("recovery has conflicting state")
        index, _existing_recovery = existing[0]
        prefix = (
            records.filtered(records[:index])
            if isinstance(records, AuthorityRecordView)
            else list(records[:index])
        )
        expected = build_recovery_evidence(
            prefix,
            repo=repo,
            admission=admission,
            capture_receipt=capture_receipt,
        )
        if id(terminal) not in authenticated_coordinator_record_ids(
            records
        ) or not _matches_enveloped_payload(terminal, expected):
            raise ProvisionalFindingError("recovery has conflicting state")
        return expected
    if terminal is None or not _eligible_terminal(terminal):
        raise ProvisionalFindingError("recovery deposit is no longer authenticated")
    if _semantic_authority_payload(admission) != _admission_payload(terminal):
        raise ProvisionalFindingError("recovery predecessor changed after admission")
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
    if not isinstance(preserved.get("review_chain_receipt"), Mapping):
        task_contract = terminal.get("task_contract")
        if isinstance(task_contract, Mapping):
            from .chain import ReviewChainError, build_review_chain_receipt

            task = dict(task_contract)
            task.setdefault("task_id", task_id)
            try:
                preserved["review_chain_receipt"] = build_review_chain_receipt(
                    task=task,
                    snapshot_sha=str(admission.get("snapshot_sha") or ""),
                    snapshot_tree_sha=str(admission.get("snapshot_tree_sha") or ""),
                    patch_identity=(
                        admission.get("patch_identity")
                        if isinstance(admission.get("patch_identity"), Mapping)
                        else None
                    ),
                    result=result,
                    finding_ids=list(persisted_receipt.get("finding_ids") or []),
                )
            except ReviewChainError as exc:
                raise ProvisionalFindingError(
                    "recovery review-chain receipt cannot be reconstructed"
                ) from exc
    return {
        **preserved,
        "type": "attempt-recovery",
        "status": "completed",
        "recovered_terminal_status": terminal.get("status"),
        "recovery_classification": "finding-deposition-only",
        "provisional_owner_id": owner,
        "finding_capture_receipt_sha256": _digest(persisted_receipt),
        "recovered_result_artifact": terminal.get("result_artifact"),
        "recovered_result_sha256": terminal.get("result_sha256"),
    }


def authorize_classified_recovery_append(
    repo: Path | str,
    records: Sequence[dict[str, object]],
    prospective: Mapping[str, object],
) -> bool:
    """Authorize one protected append, or return false for its exact replay.

    The trusted consumer must retain both package locks from this decision
    through fsync of the already-sealed ``prospective`` envelope.  This API
    validates authority and content; it does not hold signing material or write
    the consumer ledger.
    """
    from ..kernel.authority_store import assert_attempt_lifecycle_lock_held
    from ..kernel.review_state import assert_authority_ledger_lock_held

    root = _root(repo)
    assert_authority_ledger_lock_held(root)
    task_id = prospective.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ProvisionalFindingError("classified recovery task identity is invalid")
    assert_attempt_lifecycle_lock_held(task_id)

    existing = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("type") == "attempt-recovery" and record.get("task_id") == task_id
    ]
    if existing:
        if len(existing) != 1:
            raise ProvisionalFindingError("classified recovery has conflicting state")
        index, recovered = existing[0]
        prefix = (
            records.filtered(records[:index])
            if isinstance(records, AuthorityRecordView)
            else list(records[:index])
        )
        admissions = authenticated_recovery_admissions(prefix)
        admission = admissions.get(task_id)
        if admission is None:
            raise ProvisionalFindingError(
                "classified recovery admission is unavailable"
            )
        receipt = _capture_receipts(_stream_records(root)).get(
            str(admission.get("provisional_owner_id"))
        )
        if receipt is None:
            raise ProvisionalFindingError("classified recovery capture is unavailable")
        expected = build_recovery_evidence(
            prefix, repo=root, admission=admission, capture_receipt=receipt
        )
        authenticated = authenticated_coordinator_record_ids(records)
        if id(recovered) not in authenticated or not _matches_enveloped_payload(
            recovered, expected
        ):
            raise ProvisionalFindingError("classified recovery has conflicting state")
        if not _matches_enveloped_payload(prospective, expected):
            raise ProvisionalFindingError("classified recovery replay changed evidence")
        if authenticated_review_terminals(records).get(task_id) is not recovered:
            raise ProvisionalFindingError(
                "classified recovery is no longer the standing terminal"
            )
        return False

    admission = authenticated_recovery_admissions(records).get(task_id)
    if admission is None:
        raise ProvisionalFindingError("classified recovery admission is unavailable")
    receipt = _capture_receipts(_stream_records(root)).get(
        str(admission.get("provisional_owner_id"))
    )
    if receipt is None:
        raise ProvisionalFindingError("classified recovery capture is unavailable")
    expected = build_recovery_evidence(
        records, repo=root, admission=admission, capture_receipt=receipt
    )
    combined = (
        records.filtered([*records, dict(prospective)])
        if isinstance(records, AuthorityRecordView)
        else [*records, dict(prospective)]
    )
    appended = combined[-1]
    if id(appended) not in authenticated_coordinator_record_ids(combined):
        raise ProvisionalFindingError("classified recovery authority is invalid")
    if not _matches_enveloped_payload(appended, expected):
        raise ProvisionalFindingError("classified recovery evidence is invalid")
    projected = authenticated_review_terminals(combined).get(task_id)
    if projected is not appended:
        raise ProvisionalFindingError("classified recovery does not project")
    return True


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
    if authenticated_provisional_admissions(records).get(task_id) != admission:
        raise ProvisionalFindingError("publication binding owner is not authenticated")
    from ..kernel.authority_store import authority_repository_binding

    if authority_repository_binding(_root(repo)) != admission.get("repository_binding"):
        raise ProvisionalFindingError("publication repository binding changed")
    owner = str(admission.get("provisional_owner_id") or "")
    persisted_receipt = _capture_receipts(_stream_records(repo)).get(owner)
    if (
        persisted_receipt is None
        or dict(capture_receipt) != persisted_receipt
        or capture_receipt.get("type") != CAPTURE_TYPE
        or capture_receipt.get("provisional_owner_id") != owner
        or capture_receipt.get("result_sha256") != admission.get("result_sha256")
    ):
        raise ProvisionalFindingError("publication binding capture receipt is invalid")
    authenticated_provisional_findings(records, repo, admission=admission)
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
        unsigned = _semantic_authority_payload(existing)
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
    admissions = authenticated_provisional_admissions(records)
    owners = {row["provisional_owner_id"]: row for row in admissions.values()}
    result: dict[str, dict[str, object]] = {}
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
        result.setdefault(owner, row)
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
    admissions = authenticated_provisional_admissions(authority_records)
    admission = next(
        (
            row
            for row in admissions.values()
            if row.get("provisional_owner_id") == owner
        ),
        None,
    )
    if admission is None:
        raise ProvisionalFindingError("publication binding owner is not authenticated")
    findings = authenticated_provisional_findings(
        authority_records, repo, admission=admission
    )
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
    "authenticated_capture_admissions",
    "authenticated_provisional_admissions",
    "authenticated_provisional_findings",
    "authenticated_publication_bindings",
    "authenticated_recovery_admissions",
    "authorize_capture_admission_append",
    "build_capture_admission",
    "build_capture_terminal_evidence",
    "build_producer_completion",
    "build_publication_binding",
    "build_recovery_admission",
    "build_recovery_evidence",
    "capture_provisional_findings",
    "configure",
    "load_provisional_findings",
    "load_recovery_result",
    "materialize_publication_binding",
]


# Ordinary capture has a separate producer proof; recovery predicates stay closed.
from .ordinary_findings import (
    authenticated_capture_admissions,
    authenticated_provisional_admissions,
    authorize_capture_admission_append,
    build_capture_admission,
    build_capture_terminal_evidence,
    build_producer_completion,
)

# Preserve directly imported package behavior before profile configuration.
configure_kernel_seams()
