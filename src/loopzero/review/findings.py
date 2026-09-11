"""PR-scoped critical and important finding records.

Suggestions are returned to the caller as comments and are never persisted.
The merge marker closes every remaining finding for that PR; this module has
no cross-PR backlog or campaign operations.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import Profile
from ..kernel.canonical import canonical_json_bytes, canonical_record_digest


class LedgerError(RuntimeError):
    pass


class LedgerReadError(LedgerError):
    pass


class LedgerNotFound(LedgerError):
    pass


class LedgerConflict(LedgerError):
    pass


@dataclass(frozen=True)
class FindingSettings:
    root: Path
    severities: tuple[str, ...]

    @classmethod
    def from_profile(cls, profile: Profile) -> "FindingSettings":
        return cls(profile.root / profile.audit_root / "findings", profile.finding_severities)


_SETTINGS: FindingSettings | None = None


def configure(profile: Profile) -> None:
    global _SETTINGS
    _SETTINGS = FindingSettings.from_profile(profile)


def _root(repo: Path | str) -> Path:
    if _SETTINGS is not None:
        return _SETTINGS.root
    return Path(repo).resolve() / ".loopzero" / "findings"


def _pr(pr: int) -> int:
    if type(pr) is not int or pr <= 0:
        raise LedgerConflict("finding records require a positive PR number")
    return pr


def _path(repo: Path | str, pr: int) -> Path:
    return _root(repo) / f"pr-{_pr(pr)}.jsonl"


@contextmanager
def ledger_lock(repo: Path | str, pr: int):
    root = _root(repo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = root / f"pr-{_pr(pr)}.lock"
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(repo: Path | str, pr: int) -> list[dict[str, object]]:
    path = _path(repo, pr)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerReadError(f"{path}:{number}: invalid finding record") from exc
        if not isinstance(row, dict) or row.get("pr") != pr:
            raise LedgerReadError(f"{path}:{number}: malformed PR-scoped finding record")
        rows.append(row)
    return rows


def _append(repo: Path | str, pr: int, row: Mapping[str, object]) -> Path:
    path = _path(repo, pr)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".pr-{pr}-", dir=path.parent)
    try:
        previous = path.read_bytes() if path.exists() else b""
        encoded = canonical_json_bytes(dict(row)) + b"\n"
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous + encoded)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
    return path


def _finding_id(pr: int, finding: Mapping[str, object], producer_id: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes({"pr": pr, "producer_id": producer_id, "finding": dict(finding)})
    ).hexdigest()
    return f"f_{digest[:24]}"


def build_finding_capture_request(
    *, findings: Sequence[Mapping[str, object]], task_id: str | None = None,
    producer_id: str | None = None, snapshot_sha: str | None = None,
    source_head: str | None = None, producer_skill: str | None = None,
    category: str | None = None, producer_kind: str = "governed-review",
    unit_attempt_number: int | None = 1, review_intent: str = "discovery",
    pr: int | None = None, advisory: bool = False, **extra,
) -> dict[str, object]:
    if pr is None:
        candidate = extra.get("pr_number")
        pr = candidate if type(candidate) is int else None
    resolved_id = task_id or producer_id or ""
    _pr(pr)  # type: ignore[arg-type]
    if not resolved_id.strip():
        raise LedgerConflict("finding producer identity is required")
    return {
        "pr": pr, "task_id": resolved_id, "producer_id": resolved_id,
        "findings": [dict(item) for item in findings],
        "snapshot_sha": snapshot_sha, "source_head": source_head,
        "producer_skill": producer_skill, "category": category,
        "producer_kind": producer_kind, "unit_attempt_number": unit_attempt_number,
        "review_intent": review_intent, "advisory": advisory,
    }


def _operation_id(request: Mapping[str, object]) -> str:
    identity = {
        "pr": request.get("pr"), "producer_id": request.get("producer_id"),
        "unit_attempt_number": request.get("unit_attempt_number"),
    }
    return "fc_" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:32]


def capture_finding_records(
    repo: Path | str, *, request: Mapping[str, object] | None = None,
    pr: int | None = None, task_id: str | None = None,
    findings: Sequence[Mapping[str, object]] | None = None,
    candidate_records: Sequence[Mapping[str, object]] | None = None,
    source_identity: Mapping[str, object] | None = None, **metadata,
) -> dict[str, object]:
    data = dict(request or {})
    data.update(metadata)
    pr = pr if pr is not None else data.get("pr")  # type: ignore[assignment]
    task_id = task_id or str(data.get("task_id") or "")
    candidates = list(findings if findings is not None else data.get("findings") or [])
    pr = _pr(pr)  # type: ignore[arg-type]
    if not task_id:
        raise LedgerConflict("finding producer task is required")
    producer_id = f"{task_id}:{data.get('unit_attempt_number', 1)}"
    operation_id = _operation_id(data)
    with ledger_lock(repo, pr):
        existing = _read(repo, pr)
        prior_receipt = next(
            (row for row in existing if row.get("type") == "finding-capture"
             and row.get("operation_id") == operation_id), None
        )
        if prior_receipt is not None:
            return prior_receipt
        if any(row.get("type") == "pr-merged" for row in existing):
            raise LedgerConflict("findings expire at merge and cannot be appended")
        known = {row.get("finding_id"): row for row in existing if row.get("type") == "finding"}
        finding_ids: list[str] = []
        outcomes: list[dict[str, str]] = []
        writes = 0
        for index, finding in enumerate(candidates):
            if not isinstance(finding, Mapping):
                raise LedgerConflict("finding must be an object")
            severity = finding.get("severity")
            if severity == "suggestion":
                continue
            if severity not in {"critical", "important"}:
                raise LedgerConflict("only critical and important findings are persistent")
            candidate = (
                dict(candidate_records[index])
                if candidate_records is not None and index < len(candidate_records)
                else {}
            )
            finding_id = str(candidate.get("finding_id") or _finding_id(pr, finding, producer_id))
            row: dict[str, object] = {
                **candidate,
                "type": "finding", "pr": pr, "finding_id": finding_id,
                "producer_id": producer_id, "task_id": task_id,
                "severity": severity, "claim": finding.get("claim"),
                "path": finding.get("path"), "line_start": finding.get("line_start"),
                "line_end": finding.get("line_end"), "status": "open",
                "ts": datetime.now(UTC).isoformat(),
            }
            prior = known.get(finding_id)
            finding_ids.append(finding_id)
            if prior is not None:
                outcomes.append({"finding_id": finding_id, "status": "replayed"})
                continue
            _append(repo, pr, row)
            known[finding_id] = row
            writes += 1
            outcomes.append({"finding_id": finding_id, "status": "created"})
        receipt: dict[str, object] = {
            "type": "finding-capture", "pr": pr, "operation_id": operation_id,
            "producer_id": producer_id, "finding_ids": finding_ids,
            "outcomes": outcomes, "write_count": writes,
            "source_identity": dict(source_identity or {}),
            "ts": datetime.now(UTC).isoformat(),
        }
        _append(repo, pr, receipt)
    return receipt


def replay_finding_capture(repo: Path | str, request: Mapping[str, object]):
    pr = _pr(request.get("pr"))  # type: ignore[arg-type]
    operation_id = _operation_id(request)
    return next(
        (row for row in _read(repo, pr) if row.get("type") == "finding-capture"
         and row.get("operation_id") == operation_id), None
    )


def load_finding_history(repo: Path | str, *, pr: int) -> list[dict[str, object]]:
    return _read(repo, _pr(pr))


def load_finding_records(repo: Path | str, *, pr: int) -> list[dict[str, object]]:
    rows = _read(repo, _pr(pr))
    if any(row.get("type") == "pr-merged" for row in rows):
        return []
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        finding_id = row.get("finding_id")
        if isinstance(finding_id, str):
            latest[finding_id] = row
    return list(latest.values())


def append_finding_transition(
    repo: Path | str, *, pr: int, finding_id: str, status: str,
    authority: str, rationale: str,
) -> dict[str, object]:
    if status not in {"resolved", "waived"}:
        raise LedgerConflict("finding status must be resolved or waived")
    if status == "waived" and (not authority or not rationale):
        raise LedgerConflict("waiver requires merge authority and rationale")
    with ledger_lock(repo, pr):
        current = {row.get("finding_id"): row for row in load_finding_records(repo, pr=pr)}
        if finding_id not in current:
            raise LedgerNotFound(finding_id)
        row = {
            **current[finding_id], "type": "finding-transition", "status": status,
            "authority": authority, "rationale": rationale,
            "ts": datetime.now(UTC).isoformat(),
        }
        _append(repo, pr, row)
    return row


def expire_at_merge(repo: Path | str, *, pr: int, merge_sha: str) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{40}", merge_sha) is None:
        raise LedgerConflict("merge SHA must be a full lowercase commit")
    with ledger_lock(repo, pr):
        rows = _read(repo, pr)
        existing = next((row for row in rows if row.get("type") == "pr-merged"), None)
        if existing:
            if existing.get("merge_sha") != merge_sha:
                raise LedgerConflict("PR already expired at a different merge")
            return existing
        row = {"type": "pr-merged", "pr": pr, "merge_sha": merge_sha, "ts": datetime.now(UTC).isoformat()}
        _append(repo, pr, row)
        return row


def count_live_important(repo: Path | str, *, pr: int) -> int:
    return sum(
        row.get("severity") in {"critical", "important"} and row.get("status") == "open"
        for row in load_finding_records(repo, pr=pr)
    )


__all__ = [
    "LedgerConflict", "LedgerError", "LedgerNotFound", "LedgerReadError",
    "append_finding_transition", "build_finding_capture_request",
    "canonical_record_digest", "capture_finding_records", "configure",
    "count_live_important", "expire_at_merge", "load_finding_history",
    "load_finding_records", "replay_finding_capture",
]
