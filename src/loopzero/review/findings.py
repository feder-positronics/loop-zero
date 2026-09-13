"""Shared, append-only Finding Ledger mechanics.

Finding state is stored in ``.audit/findings``.  Coordination facts are stored
in ``.audit/finding-operations``.  Both streams are serialized by the primary
checkout's ``.audit/findings/.ledger.lock`` so linked worktrees cannot create a
shadow authority.

Merge expiry deliberately excludes coordinator-signed inline terminals.  They
can authenticate controller lifecycle facts, but only a registered dispatcher
attempt proves that the delivery bound to the finding's ``(run_id, PR)`` merged.

That guarantee is only as strong as the kernel's registration projection.  For
legacy compatibility the kernel admits *proofless* attempt-start records as
registrations when they sit in the pre-cutover ledger prefix (every record
before the first ``coordinator-authority-cutover`` attempt).  In a ledger that
has no cutover record at all, that prefix is the whole ledger, so an unsigned
start could register an arbitrary dispatcher key.  Consumers must therefore
have completed the coordinator cutover before relying on merge expiry; after
the cutover record only coordinator-authenticated registrations count, and a
proofless start no longer expires anything.
"""

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Profile

FINDINGS_DIR = Path(".audit/findings")
OPERATIONS_DIR = Path(".audit/finding-operations")
RF_AUDIT_BACKLOG_CEILING = 130
FINDING_CAPTURE_OPERATION_TYPE = "finding-capture-operation"
FINDING_CAPTURE_OPERATION_VERSION = "finding-capture-v1"
FAILED_REVIEW_RETIREMENT_OPERATION_TYPE = "failed-review-retirement-operation"
FAILED_REVIEW_RETIREMENT_OPERATION_VERSION = "failed-review-retirement-v1"
FINDING_CAPTURE_RESULT_FIELDS = (
    "severity",
    "claim",
    "path",
    "line_start",
    "line_end",
)

_SEVERITY_RANK = {"suggestion": 1, "important": 2, "critical": 3}
_SECURITY_PATH_PATTERNS = (
    "*dependencies*.py",
    "*middleware*",
    "*/integrations/*",
    "*config.py",
    "*.env*",
    "pyproject.toml",
    "package.json",
)
_CONFIGURED_ROOT: Path | None = None
_REQUIRE_PR_SCOPE = False


def configure(profile: Profile) -> None:
    """Bind consumer-owned ledger paths and review vocabulary."""
    global FINDINGS_DIR, OPERATIONS_DIR, _SEVERITY_RANK, _SECURITY_PATH_PATTERNS
    global _CONFIGURED_ROOT, _REQUIRE_PR_SCOPE
    _CONFIGURED_ROOT = Path(profile.root).expanduser().resolve()
    _REQUIRE_PR_SCOPE = True
    FINDINGS_DIR = profile.audit_root / "findings"
    OPERATIONS_DIR = profile.audit_root / "finding-operations"
    _SEVERITY_RANK = {
        severity: index
        for index, severity in enumerate(reversed(profile.finding_severities), start=1)
    }
    _SECURITY_PATH_PATTERNS = tuple(getattr(profile, "security_patterns", ())) or (
        "*dependencies*.py",
        "*middleware*",
        "*/integrations/*",
        "*config.py",
        "*.env*",
        "pyproject.toml",
        "package.json",
    )


class LedgerError(RuntimeError):
    """Base error for a governed ledger operation."""


class LedgerReadError(LedgerError):
    """The primary append-only stream could not be read safely."""


class LedgerNotFound(LedgerError):
    """An exact finding, lease, or reservation does not exist."""


class LedgerConflict(LedgerError):
    """A compare-and-swap or operation-id precondition failed."""


class LedgerLeaseConflict(LedgerConflict):
    """A finding is owned by another active lease."""


class LedgerAuthorityError(LedgerError):
    """The supplied evidence cannot authorize the requested operation."""


# Audit-campaign identity stays in the consumer.

def _run_git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:200]
        raise LedgerReadError(f"not a git checkout: {path} ({detail})")
    return completed.stdout.strip()


def primary_repo_root(path: Path | str, *, require_git_primary: bool = False) -> Path:
    """Resolve the primary checkout, refusing local fallback for governed work."""
    checkout = Path(path).expanduser().resolve()
    try:
        common_dir = Path(
            _run_git(
                checkout,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        ).resolve()
    except LedgerReadError:
        # Unit/telemetry fixtures may be explicit standalone audit roots.
        # Governed discovery must instead prove Git's common directory: a
        # caller-local `.audit` is never authority when that proof fails.
        if not require_git_primary and (checkout / ".audit").is_dir():
            return checkout
        raise
    return common_dir.parent


def _primary(path: Path | str, *, require_git_primary: bool = False) -> Path:
    if _CONFIGURED_ROOT is not None and Path(path).expanduser().resolve() == _CONFIGURED_ROOT:
        return _CONFIGURED_ROOT
    return primary_repo_root(path, require_git_primary=require_git_primary)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise LedgerConflict(f"ledger payload is not JSON-serializable: {exc}") from exc


def canonical_record_digest(record: Mapping[str, object]) -> str:
    """Hash every persisted field of a latest record using canonical JSON."""
    return hashlib.sha256(_canonical_json(dict(record)).encode("utf-8")).hexdigest()


def _request_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _read_stream(
    directory: Path,
    *,
    require_existing: bool,
    stream_name: str,
    strict_malformed: bool = False,
) -> list[dict[str, object]]:
    if not directory.is_dir():
        if require_existing:
            raise LedgerReadError(f"primary {stream_name} does not exist: {directory}")
        return []
    paths = sorted(directory.glob("*.jsonl"))
    if require_existing and not paths:
        raise LedgerReadError(f"primary {stream_name} does not exist: {directory}")
    records: list[dict[str, object]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LedgerReadError(
                f"cannot read {stream_name} shard {path}: {exc}"
            ) from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if require_existing or strict_malformed:
                    raise LedgerReadError(
                        f"invalid JSON in {stream_name} shard {path}:{line_number}"
                    ) from exc
                continue
            if not isinstance(record, dict):
                if require_existing or strict_malformed:
                    raise LedgerReadError(
                        f"invalid record in {stream_name} shard {path}:{line_number}"
                    )
                continue
            records.append(record)
    return records


def _validate_capture_integrity(records: Sequence[Mapping[str, object]]) -> None:
    """Reject capture membership rows without exactly one terminal receipt."""
    receipts: Counter[str] = Counter()
    members: set[str] = set()
    for record in records:
        if record.get("type") == FINDING_CAPTURE_OPERATION_TYPE:
            operation_id = record.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise LedgerReadError("finding capture receipt has no operation_id")
            receipts[operation_id] += 1
        capture_operation_id = record.get("capture_operation_id")
        capture_record_kind = record.get("capture_record_kind")
        if capture_operation_id is None and capture_record_kind is None:
            continue
        if (
            not isinstance(capture_operation_id, str)
            or not capture_operation_id
            or capture_record_kind not in {"finding", "promotion"}
        ):
            raise LedgerReadError("invalid finding capture membership marker")
        members.add(capture_operation_id)
    duplicate = next((key for key, count in receipts.items() if count != 1), None)
    if duplicate is not None:
        raise LedgerReadError(
            f"finding capture {duplicate!r} has more than one terminal receipt"
        )
    missing = next((key for key in members if receipts[key] != 1), None)
    if missing is not None:
        raise LedgerReadError(
            f"finding capture {missing!r} has no matching terminal receipt"
        )


def load_finding_history(
    repo: Path | str,
    *,
    pr: int | None = None,
    require_existing_for_discovery: bool = False,
    strict_malformed: bool = False,
) -> list[dict[str, object]]:
    """Load the complete finding stream in append order."""
    primary = _primary(repo, require_git_primary=require_existing_for_discovery)
    records = _read_stream(
        primary / FINDINGS_DIR,
        require_existing=require_existing_for_discovery,
        stream_name="Finding Ledger",
        strict_malformed=strict_malformed,
    )
    if strict_malformed:
        _validate_capture_integrity(records)
    if pr is not None:
        if type(pr) is not int or pr <= 0:
            raise LedgerConflict("finding records require a positive PR number")
        records = [record for record in records if record.get("pr") == pr]
    return records


def load_finding_records(
    repo: Path | str,
    *,
    pr: int | None = None,
    require_existing_for_discovery: bool = False,
    strict_malformed: bool = False,
) -> list[dict[str, object]]:
    """Load latest-wins finding records from the primary checkout."""
    latest: dict[str, dict[str, object]] = {}
    for record in load_finding_history(
        repo,
        pr=pr,
        require_existing_for_discovery=require_existing_for_discovery,
        strict_malformed=strict_malformed,
    ):
        finding_id = record.get("finding_id")
        if isinstance(finding_id, str) and finding_id:
            latest[finding_id] = record
    records = list(latest.values())
    bound_runs = {
        (record["delivery_run_id"], record.get("pr"))
        for record in records
        if isinstance(record.get("delivery_run_id"), str)
    }
    if not bound_runs:
        return records
    # Expiry is a read projection of the kernel-authenticated controller
    # stream.  The local skill-run log remains useful lifecycle telemetry, but
    # its unsigned rows cannot settle review debt.
    try:
        from ..kernel.run_identity import run_delivery_contract
        from ..kernel.run_log import load_entries

        run_entries = load_entries(_primary(repo) / ".audit/skill-runs")
        authority = _registered_dispatcher_delivery_terminals(_primary(repo))
    except (OSError, RuntimeError, ValueError):
        return records  # Missing or contradictory authority retains the debt.
    expired: set[tuple[object, object]] = set()
    for run_id, run_pr in bound_runs:
        try:
            contract = run_delivery_contract(run_entries, run_id)
        except ValueError:
            continue
        if (
            contract == "loop-zero-v1"
            and type(run_pr) is int
            and any(
                row.get("run_id") == run_id
                and row.get("pr") == run_pr
                and row.get("outcome") == "merged"
                and row.get("type") == "attempt-terminal"
                for row in authority
            )
        ):
            expired.add((run_id, run_pr))
    return [
        record
        for record in records
        if (record.get("delivery_run_id"), record.get("pr")) not in expired
    ]


def _authenticated_delivery_run_records(repo: Path) -> list[dict[str, object]]:
    """Load the delivery-controller projection from protected authority state."""
    from ..kernel.authority_store import load_authority_records
    from .authority import delivery_controller_records

    return delivery_controller_records(load_authority_records(repo, 30))


def _registered_dispatcher_delivery_terminals(
    repo: Path,
) -> list[dict[str, object]]:
    """Load authenticated terminals from registered dispatcher attempts."""
    from ..kernel.authority_store import load_authority_records
    from .authority import registered_dispatcher_delivery_controller_records

    return registered_dispatcher_delivery_controller_records(
        load_authority_records(repo, 30)
    )


def authenticated_delivery_run_pr(repo: Path, run_id: str) -> int:
    """Resolve one run's PR solely from authenticated controller records."""
    prs = {
        row["pr"]
        for row in _authenticated_delivery_run_records(_primary(repo))
        if row.get("run_id") == run_id
        and type(row.get("pr")) is int
        and int(row["pr"]) > 0
    }
    if len(prs) != 1:
        raise LedgerConflict("delivery run has no unique authenticated PR binding")
    return int(prs.pop())


def load_operation_history(repo: Path | str) -> list[dict[str, object]]:
    """Load coordination history; absence is valid before the first operation."""
    primary = _primary(repo)
    return _read_stream(
        primary / OPERATIONS_DIR,
        require_existing=False,
        stream_name="Finding Ledger operations",
        strict_malformed=True,
    )


def count_live_important(
    repo: Path | str,
    *,
    pr: int | None = None,
    require_existing_for_discovery: bool = False,
) -> int:
    """Count open critical/important findings, including candidates."""
    return sum(
        record.get("state") == "open"
        and record.get("severity") in {"critical", "important"}
        for record in load_finding_records(
            repo, pr=pr, require_existing_for_discovery=require_existing_for_discovery
        )
    )


@contextmanager
def ledger_lock(
    repo: Path | str,
    *,
    require_existing_for_discovery: bool = False,
) -> Iterator[Path]:
    """Lock the Git-proven canonical primary stream for every mutation."""
    primary = _primary(repo, require_git_primary=True)
    directory = primary / FINDINGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".ledger.lock"
    try:
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                if require_existing_for_discovery:
                    _read_stream(
                        directory,
                        require_existing=True,
                        stream_name="Finding Ledger",
                    )
                yield primary
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise LedgerReadError(
            f"cannot lock Finding Ledger at {lock_path}: {exc}"
        ) from exc


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(now: datetime | None = None) -> str:
    return (now or _now()).isoformat(timespec="seconds")


def _append_jsonl(
    primary: Path, directory_name: Path, record: Mapping[str, object]
) -> None:
    directory = primary / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    now = _parse_timestamp(record.get("ts")) or _now()
    path = directory / f"{now:%Y-%m-%d}.jsonl"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n"
            )
            handle.flush()
    except OSError as exc:
        raise LedgerReadError(f"cannot append ledger record to {path}: {exc}") from exc


def _all_operation_records(primary: Path) -> Iterator[dict[str, object]]:
    finding_records = _read_stream(
        primary / FINDINGS_DIR,
        require_existing=False,
        stream_name="Finding Ledger",
        strict_malformed=True,
    )
    _validate_capture_integrity(finding_records)
    yield from finding_records
    yield from _read_stream(
        primary / OPERATIONS_DIR,
        require_existing=False,
        stream_name="Finding Ledger operations",
        strict_malformed=True,
    )


def _operation_replay(
    primary: Path,
    *,
    operation_id: str,
    request_digest: str,
    terminal_type: str | None = None,
) -> dict[str, object] | None:
    if not operation_id.strip():
        raise LedgerConflict("operation_id must be non-empty")
    matches = [
        record
        for record in _all_operation_records(primary)
        if record.get("operation_id") == operation_id
    ]
    if terminal_type is not None:
        typed = [record for record in matches if record.get("type") == terminal_type]
        if len(typed) > 1:
            raise LedgerReadError(
                f"operation_id {operation_id!r} has multiple {terminal_type!r} receipts"
            )
        if matches and not typed:
            raise LedgerConflict(
                f"operation_id {operation_id!r} belongs to a different operation type"
            )
        matches = typed
    if matches:
        record = matches[0]
        if record.get("operation_request_digest") != request_digest:
            raise LedgerConflict(
                f"operation_id {operation_id!r} was already used with a different payload"
            )
        return record
    return None


def build_finding_capture_request(
    *,
    producer_kind: str = "governed-review",
    producer_id: str = "",
    unit_attempt_number: int | None = 1,
    producer_skill: str = "",
    category: str = "",
    advisory: bool = False,
    findings: Sequence[Mapping[str, object]],
    pr: int | None = None,
    task_id: str | None = None,
    **extra: object,
) -> dict[str, object]:
    """Build the snapshot-independent request identity for one producer result."""
    if pr is None and type(extra.get("pr_number")) is int:
        pr = int(extra["pr_number"])
    if _REQUIRE_PR_SCOPE and (type(pr) is not int or pr <= 0):
        raise LedgerConflict("finding records require a positive PR number")
    producer_id = task_id or producer_id
    if producer_kind not in {
        "governed-review",
        "local-review",
        "late-review-convergence",
    }:
        raise LedgerConflict(f"unsupported finding producer kind {producer_kind!r}")
    required = {
        "producer_id": producer_id,
        "producer_skill": producer_skill,
        "category": category,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise LedgerConflict(
            f"finding capture identity is missing: {', '.join(sorted(missing))}"
        )
    if producer_kind == "governed-review":
        if (
            not isinstance(unit_attempt_number, int)
            or isinstance(unit_attempt_number, bool)
            or unit_attempt_number < 1
        ):
            raise LedgerConflict(
                "governed finding capture requires a positive unit_attempt_number"
            )
    elif unit_attempt_number is not None:
        raise LedgerConflict(
            f"{producer_kind} finding capture has no unit_attempt_number"
        )
    if producer_kind == "late-review-convergence" and advisory:
        raise LedgerConflict("late-review convergence findings are authoritative")
    normalized_findings = [
        {field: finding.get(field) for field in FINDING_CAPTURE_RESULT_FIELDS}
        for finding in findings
    ]
    if not normalized_findings:
        raise LedgerConflict("finding capture requires at least one finding")
    request = {
        "type": FINDING_CAPTURE_OPERATION_TYPE,
        "producer_kind": producer_kind,
        "producer_id": producer_id,
        "unit_attempt_number": unit_attempt_number,
        "producer_skill": producer_skill,
        "category": category,
        "advisory": advisory,
        "findings": normalized_findings,
    }
    if pr is not None:
        request["pr"] = pr
    return request


def finding_capture_operation_id(request: Mapping[str, object]) -> str:
    """Derive the stable producer-operation identity, excluding result content."""
    identity = {
        "pr": request.get("pr"),
        "producer_kind": request.get("producer_kind"),
        "producer_id": request.get("producer_id"),
        "unit_attempt_number": request.get("unit_attempt_number"),
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"{FINDING_CAPTURE_OPERATION_VERSION}-{digest}"


def _finding_capture_replay(
    primary: Path, request: Mapping[str, object]
) -> dict[str, object] | None:
    return _operation_replay(
        primary,
        operation_id=finding_capture_operation_id(request),
        request_digest=_request_digest(request),
        terminal_type=FINDING_CAPTURE_OPERATION_TYPE,
    )


def replay_finding_capture(
    repo: Path | str, *, request: Mapping[str, object]
) -> dict[str, object] | None:
    """Return an exact capture replay before a producer creates new source evidence."""
    with ledger_lock(repo) as primary:
        return _finding_capture_replay(primary, request)


def _validate_capture_candidates(
    request: Mapping[str, object], candidate_records: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    findings = request.get("findings")
    if not isinstance(findings, list) or len(findings) != len(candidate_records):
        raise LedgerConflict(
            "capture candidate count must match the ordered producer findings"
        )
    expected_advisory = request.get("advisory") is True
    prepared: list[dict[str, object]] = []
    for index, (finding, candidate) in enumerate(
        zip(findings, candidate_records, strict=True), start=1
    ):
        if not isinstance(finding, Mapping):
            raise LedgerConflict(f"capture finding {index} must be a mapping")
        record = dict(candidate)
        finding_id = record.get("finding_id")
        claim = record.get("claim")
        severity = record.get("severity")
        if not isinstance(finding_id, str) or not finding_id:
            raise LedgerConflict(f"capture candidate {index} has no finding_id")
        if not isinstance(claim, str) or not claim:
            raise LedgerConflict(f"capture candidate {index} has no claim")
        if severity not in _SEVERITY_RANK:
            raise LedgerConflict(f"unsupported severity {severity!r}")
        if claim != finding.get("claim") or severity != finding.get("severity"):
            raise LedgerConflict(
                f"capture candidate {index} does not match the producer result"
            )
        if record.get("review_task_id") != request.get("producer_id"):
            raise LedgerConflict(
                f"capture candidate {index} has the wrong producer identity"
            )
        if (record.get("advisory") is True) != expected_advisory:
            raise LedgerConflict(f"capture candidate {index} has the wrong mode")
        if record.get("state") != "open" or record.get("disposition") is not None:
            raise LedgerConflict(
                f"capture candidate {index} must begin as an open finding"
            )
        _canonical_json(record)
        prepared.append(record)
    return prepared


def _replace_jsonl_segment(
    primary: Path, records: Sequence[Mapping[str, object]], *, now: datetime
) -> None:
    """Publish a complete capture batch through one durable same-directory rename."""
    directory = primary / FINDINGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%Y-%m-%d}.jsonl"
    try:
        existing = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        raise LedgerReadError(f"cannot read ledger segment {path}: {exc}") from exc
    if existing and not existing.endswith(b"\n"):
        raise LedgerReadError(f"cannot extend unterminated ledger segment {path}")
    appended = "".join(
        json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n"
        for record in records
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(existing)
            handle.write(appended)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise LedgerReadError(f"cannot replace ledger segment {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def capture_finding_records(
    repo: Path | str,
    *,
    request: Mapping[str, object],
    candidate_records: Sequence[Mapping[str, object]] | None = None,
    source_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Atomically deposit or replay one complete structured finding result."""
    operation_id = finding_capture_operation_id(request)
    request_digest = _request_digest(request)
    validation_request = request
    if candidate_records is None:
        pr = request.get("pr")
        if type(pr) is not int or pr <= 0:
            raise LedgerConflict("finding records require a positive PR number")
        retained = [
            finding
            for finding in request.get("findings", [])
            if isinstance(finding, Mapping) and finding.get("severity") != "suggestion"
        ]
        validation_request = {**request, "findings": retained}
        candidate_records = []
        for finding in retained:
            finding_id = "f_" + hashlib.sha256(
                _canonical_json(
                    {
                        "pr": pr,
                        "producer_id": request.get("producer_id"),
                        "finding": dict(finding),
                    }
                ).encode("utf-8")
            ).hexdigest()[:24]
            candidate_records.append(
                {
                    "pr": pr,
                    "finding_id": finding_id,
                    "review_task_id": request.get("producer_id"),
                    "severity": finding.get("severity"),
                    "claim": finding.get("claim"),
                    "path": finding.get("path"),
                    "line_start": finding.get("line_start"),
                    "line_end": finding.get("line_end"),
                    "state": "open",
                    "status": "open",
                    "disposition": None,
                    "advisory": request.get("advisory") is True,
                }
            )
        source_identity = source_identity or {}
    with ledger_lock(repo) as primary:
        replay = _operation_replay(
            primary,
            operation_id=operation_id,
            request_digest=request_digest,
            terminal_type=FINDING_CAPTURE_OPERATION_TYPE,
        )
        if replay is not None:
            return replay
        candidates = _validate_capture_candidates(validation_request, candidate_records)
        _canonical_json(dict(source_identity or {}))
        latest = _latest_by_id(primary, strict_malformed=True)
        now = _now()
        timestamp = _timestamp(now)
        batch_records: list[dict[str, object]] = []
        outcomes: list[dict[str, str]] = []
        finding_ids: list[str] = []
        candidate_ids = [str(candidate["finding_id"]) for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise LedgerConflict("capture candidate finding IDs must be unique")
        for candidate in candidates:
            finding_id = str(candidate["finding_id"])
            finding_ids.append(finding_id)
            existing = latest.get(finding_id)
            promote_advisory = (
                existing is not None
                and existing.get("advisory") is True
                and request.get("advisory") is not True
            )
            reopen_failed_review = (
                existing is not None
                and existing.get("state") == "stale"
                and existing.get("disposition") == "failed-review-superseded"
                and existing.get("review_task_id") != request.get("producer_id")
            )
            if existing is not None and not promote_advisory and not reopen_failed_review:
                outcomes.append({"finding_id": finding_id, "status": "replayed"})
                continue
            if promote_advisory:
                existing_severity = existing.get("severity")
                candidate_severity = candidate.get("severity")
                if (
                    existing_severity not in _SEVERITY_RANK
                    or candidate_severity not in _SEVERITY_RANK
                ):
                    raise LedgerConflict(
                        "advisory promotion requires supported severity values"
                    )
                promoted_severity = max(
                    (str(existing_severity), str(candidate_severity)),
                    key=_SEVERITY_RANK.__getitem__,
                )
                persisted = {
                    key: value
                    for key, value in existing.items()
                    if key
                    not in {
                        "operation_id",
                        "operation_request_digest",
                        "capture_operation_id",
                        "capture_record_kind",
                    }
                }
                persisted.update(candidate)
                persisted["severity"] = promoted_severity
                persisted["capture_record_kind"] = "promotion"
                status = "promoted"
            elif reopen_failed_review:
                assert existing is not None
                existing_severity = existing.get("severity")
                candidate_severity = candidate.get("severity")
                if (
                    existing_severity not in _SEVERITY_RANK
                    or candidate_severity not in _SEVERITY_RANK
                ):
                    raise LedgerConflict(
                        "failed-review reopening requires supported severity values"
                    )
                persisted = dict(candidate)
                persisted["severity"] = max(
                    (str(existing_severity), str(candidate_severity)),
                    key=_SEVERITY_RANK.__getitem__,
                )
                if existing.get("original_severity") in _SEVERITY_RANK:
                    persisted["original_severity"] = existing["original_severity"]
                persisted["capture_record_kind"] = "finding"
                status = "reopened"
            else:
                persisted = dict(candidate)
                persisted["capture_record_kind"] = "finding"
                status = "created"
            persisted.update(
                {
                    "ts": timestamp,
                    "capture_operation_id": operation_id,
                }
            )
            latest[finding_id] = persisted
            batch_records.append(persisted)
            outcomes.append({"finding_id": finding_id, "status": status})
        receipt = {
            "type": FINDING_CAPTURE_OPERATION_TYPE,
            "pr": request.get("pr"),
            "operation_id": operation_id,
            "operation_request_digest": request_digest,
            "producer_kind": request.get("producer_kind"),
            "producer_id": request.get("producer_id"),
            "unit_attempt_number": request.get("unit_attempt_number"),
            "producer_skill": request.get("producer_skill"),
            "category": request.get("category"),
            "advisory": request.get("advisory"),
            "source_identity": dict(source_identity or {}),
            "result_digest": _request_digest({"findings": request.get("findings")}),
            "finding_ids": finding_ids,
            "outcomes": outcomes,
            "write_count": len(batch_records),
            "ts": timestamp,
        }
        _replace_jsonl_segment(primary, [*batch_records, receipt], now=now)
        return receipt


def retire_failed_review_findings(
    repo: Path | str,
    *,
    producer_id: str,
    expected_finding_ids: Sequence[str],
    supersession_digest: str,
    verdict_digest: str,
) -> dict[str, object]:
    """Atomically stale every still-open finding from one failed review.

    The caller authenticates the dispatch supersession and independent fail
    verdict. This boundary binds those exact authority records to one stable
    Finding Ledger transaction. A crash before the same-directory rename
    publishes nothing; a retry replays the completed receipt.
    """
    if not isinstance(producer_id, str) or not producer_id.strip():
        raise LedgerConflict("failed review producer_id must be non-empty")
    finding_ids = sorted(expected_finding_ids)
    if any(not isinstance(item, str) or not item for item in finding_ids) or len(
        finding_ids
    ) != len(set(finding_ids)):
        raise LedgerConflict("failed review finding IDs must be non-empty and unique")
    for name, digest in (
        ("supersession", supersession_digest),
        ("verdict", verdict_digest),
    ):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise LedgerConflict(
                f"failed review {name} digest must be 64 lowercase hex"
            )
    request = {
        "type": FAILED_REVIEW_RETIREMENT_OPERATION_TYPE,
        "producer_id": producer_id,
        "finding_ids": finding_ids,
        "supersession_digest": supersession_digest,
        "verdict_digest": verdict_digest,
    }
    request_digest = _request_digest(request)
    identity_digest = hashlib.sha256(producer_id.encode("utf-8")).hexdigest()
    operation_id = f"{FAILED_REVIEW_RETIREMENT_OPERATION_VERSION}-{identity_digest}"
    with ledger_lock(repo) as primary:
        replay = _operation_replay(
            primary,
            operation_id=operation_id,
            request_digest=request_digest,
            terminal_type=FAILED_REVIEW_RETIREMENT_OPERATION_TYPE,
        )
        if replay is not None:
            return replay
        latest = _latest_by_id(primary, strict_malformed=True)
        missing_finding_ids = sorted(set(finding_ids) - latest.keys())
        producer_finding_ids = {
            finding_id
            for finding_id, record in latest.items()
            if record.get("review_task_id") == producer_id
        }
        unexpected_finding_ids = sorted(producer_finding_ids - set(finding_ids))
        if missing_finding_ids or unexpected_finding_ids:
            raise LedgerConflict(
                "failed review finding batch does not match its capture receipt"
            )
        leases = _active_leases(primary)
        now = _now()
        timestamp = _timestamp(now)
        transitions: list[dict[str, object]] = []
        outcomes: list[dict[str, str]] = []
        for finding_id in finding_ids:
            existing = latest[finding_id]
            if existing.get("review_task_id") != producer_id:
                outcomes.append(
                    {"finding_id": finding_id, "status": "retained-other-producer"}
                )
                continue
            if existing.get("state") != "open":
                outcomes.append({"finding_id": finding_id, "status": "unchanged"})
                continue
            lease = leases.get(finding_id)
            if lease is not None:
                raise LedgerLeaseConflict(
                    f"finding {finding_id!r} has an active lease owned by "
                    f"{lease.get('owner')!r}"
                )
            updated = {
                key: value
                for key, value in existing.items()
                if key
                not in {
                    "operation_id",
                    "operation_request_digest",
                    "capture_operation_id",
                    "capture_record_kind",
                }
            }
            updates = {
                "state": "stale",
                "disposition": "failed-review-superseded",
                "failed_review_supersession_digest": supersession_digest,
                "failed_review_verdict_digest": verdict_digest,
            }
            updated.update(updates)
            updated.update(
                {
                    "finding_id": finding_id,
                    "operation_id": f"{operation_id}:{finding_id}",
                    "operation_request_digest": request_digest,
                    "ts": timestamp,
                }
            )
            _validate_terminal_provenance(updated, updates, latest.keys(), existing)
            transitions.append(updated)
            outcomes.append({"finding_id": finding_id, "status": "stale"})
        receipt: dict[str, object] = {
            **request,
            "operation_id": operation_id,
            "operation_request_digest": request_digest,
            "outcomes": outcomes,
            "write_count": len(transitions),
            "ts": timestamp,
        }
        _replace_jsonl_segment(primary, [*transitions, receipt], now=now)
        return receipt


def _latest_by_id(
    primary: Path,
    *,
    require_existing_for_discovery: bool = False,
    strict_malformed: bool = True,
) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    records = _read_stream(
        primary / FINDINGS_DIR,
        require_existing=require_existing_for_discovery,
        stream_name="Finding Ledger",
        strict_malformed=strict_malformed,
    )
    if strict_malformed:
        _validate_capture_integrity(records)
    for record in records:
        finding_id = record.get("finding_id")
        if isinstance(finding_id, str):
            latest[finding_id] = record
    return latest


def _owner_identity(owner: Mapping[str, object] | str | None) -> object:
    if owner is None or isinstance(owner, str):
        return owner
    return {
        key: str(owner[key])
        for key in ("coordinator_id", "run_id", "worker_session_id")
        if key in owner
    }


def _require_owner_fields(owner: Mapping[str, object]) -> None:
    missing = [
        key
        for key in ("coordinator_id", "run_id", "worker_session_id")
        if not isinstance(owner.get(key), str) or not str(owner[key]).strip()
    ]
    if missing:
        raise LedgerConflict(f"owner identity is missing: {', '.join(missing)}")


def _owner_matches(
    lease: Mapping[str, object], owner: Mapping[str, object] | str | None
) -> bool:
    if isinstance(owner, str):
        return owner == lease.get("lease_id")
    return _owner_identity(owner) == lease.get("owner")


def _transition_owner_identity(
    lease_owner: Mapping[str, object] | str | None,
) -> dict[str, str] | None:
    """Normalize transition authority without accepting a lease ID as a capability."""
    if lease_owner is None:
        return None
    if not isinstance(lease_owner, Mapping):
        raise LedgerAuthorityError(
            "finding transitions require a full owner Mapping, not a lease_id"
        )
    try:
        _require_owner_fields(lease_owner)
    except LedgerConflict as exc:
        raise LedgerAuthorityError(
            "finding transitions require a full owner Mapping"
        ) from exc
    return {
        key: str(lease_owner[key])
        for key in ("coordinator_id", "run_id", "worker_session_id")
    }


def _validate_transition_severity(
    existing: Mapping[str, object], updates: Mapping[str, object]
) -> None:
    new_severity = updates.get("severity")
    if new_severity is None or new_severity == existing.get("severity"):
        for field in ("original_severity", "severity_correction"):
            if field in updates and updates[field] != existing.get(field):
                raise LedgerConflict(
                    "severity correction does not preserve its assessment lineage"
                )
        return
    correction = updates.get("severity_correction")
    if not isinstance(correction, Mapping):
        raise LedgerAuthorityError(
            "severity changes must use the governed severity-correction path"
        )
    if new_severity not in _SEVERITY_RANK:
        raise LedgerConflict(f"unsupported severity {new_severity!r}")
    preserved_original = existing.get("original_severity") or existing.get("severity")
    if (
        updates.get("original_severity") != preserved_original
        or correction.get("from") != existing.get("severity")
        or correction.get("to") != new_severity
    ):
        raise LedgerConflict(
            "severity correction does not preserve its assessment lineage"
        )
    authority = closure_authority_severity(existing)
    if _SEVERITY_RANK.get(authority, 0) >= _SEVERITY_RANK[
        "important"
    ] and not correction.get("decision_record"):
        raise LedgerAuthorityError(
            "important severity corrections require a Decision Record"
        )
    if (authority == "critical" or _is_security_path(existing)) and correction.get(
        "owner_approved"
    ) is not True:
        raise LedgerAuthorityError(
            "critical or security-path severity corrections require owner authority"
        )


TERMINAL_STATES = frozenset({"addressed", "waived", "stale"})
LINEAGE_REF_FIELDS = (
    "duplicate_of",
    "superseded_by",
    "same_defect_of",
    "canonical_finding_id",
)


def declared_commands_sha256(commands: Sequence[str]) -> str:
    """Return the stable digest used by deterministic evidence receipts."""
    payload = json.dumps(
        list(commands), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_deterministic_evidence(
    value: object, *, existing: Mapping[str, object] | None
) -> bool:
    if not isinstance(value, Mapping):
        return False
    required = {
        "schema_version",
        "evidence_id",
        "target_snapshot_sha",
        "target_tree_sha",
        "commands_sha256",
        "results",
        "validated_at",
    }
    if set(value) not in (
        required | {"finding_bindings"},
        required if existing is None else set(),
    ):
        return False
    if value.get("schema_version") != "deterministic-finding-evidence-v1":
        return False
    evidence_id = value.get("evidence_id")
    if not isinstance(evidence_id, str) or not re.fullmatch(
        r"fde_[0-9a-f]{24}", evidence_id
    ):
        return False
    for field, length in (
        ("target_snapshot_sha", 40),
        ("target_tree_sha", 40),
        ("commands_sha256", 64),
    ):
        item = value.get(field)
        if not isinstance(item, str) or not re.fullmatch(
            rf"[0-9a-f]{{{length}}}", item
        ):
            return False
    results = value.get("results")
    if not isinstance(results, list) or not results or len(results) > 32:
        return False
    declared: list[str] = []
    for result in results:
        if not isinstance(result, Mapping) or set(result) != {
            "declared_command",
            "executed_command",
            "exit_code",
        }:
            return False
        declared_command = result.get("declared_command")
        executed_command = result.get("executed_command")
        if (
            not isinstance(declared_command, str)
            or not declared_command
            or not isinstance(executed_command, str)
            or not executed_command
            or result.get("exit_code") != 0
            or isinstance(result.get("exit_code"), bool)
        ):
            return False
        declared.append(declared_command)
    if value.get("commands_sha256") != declared_commands_sha256(declared):
        return False
    bindings = value.get("finding_bindings")
    if bindings is None and existing is None:
        bindings = []
    if not isinstance(bindings, list) or len(bindings) > 64:
        return False
    if existing is not None and not bindings:
        return False
    matching = []
    for binding in bindings:
        if not isinstance(binding, Mapping) or set(binding) != {
            "finding_id",
            "finding_record_digest",
            "source_snapshot_sha",
            "source_tree_sha",
            "anchor",
        }:
            return False
        if binding.get("finding_id") == (
            existing.get("finding_id") if existing is not None else None
        ):
            matching.append(binding)
        if not isinstance(
            binding.get("finding_record_digest"), str
        ) or not re.fullmatch(
            r"[0-9a-f]{64}", str(binding.get("finding_record_digest"))
        ):
            return False
        if not isinstance(binding.get("anchor"), Mapping):
            return False
        for field in ("source_snapshot_sha", "source_tree_sha"):
            if not isinstance(binding.get(field), str) or not re.fullmatch(
                r"[0-9a-f]{40}", str(binding.get(field))
            ):
                return False
    if existing is not None:
        if len(matching) != 1:
            return False
        binding = matching[0]
        if binding.get("finding_record_digest") != canonical_record_digest(existing):
            return False
        if any(
            binding.get(binding_field) != existing.get(record_field)
            for binding_field, record_field in (
                ("source_snapshot_sha", "snapshot_sha"),
                ("source_tree_sha", "snapshot_tree_sha"),
            )
        ):
            return False
        if dict(binding["anchor"]) != existing.get("anchor"):
            return False
        if value.get("target_tree_sha") == binding.get("source_tree_sha"):
            return False
    validated_at = value.get("validated_at")
    if not isinstance(validated_at, str):
        return False
    try:
        timestamp = datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None


def _validate_terminal_provenance(
    merged: Mapping[str, object],
    updates: Mapping[str, object],
    known_finding_ids: Collection[str],
    existing: Mapping[str, object] | None,
) -> None:
    """Reject a terminal transition whose provenance is incomplete (#3418 P4).

    Provenance-at-write replaces provenance-by-repair: the 2026-08 restore
    wave existed because closures could land without their fix/waiver/lineage
    bindings and doc commits had to reconstruct them later. Every rejection
    names the exact missing field (Fail Fast). Runs inside the ledger lock as
    part of the fenced validate+append operation — never validate outside it.
    """
    state = merged.get("state")
    for field in LINEAGE_REF_FIELDS:
        if field in updates:
            target = updates[field]
            if target not in known_finding_ids:
                raise LedgerConflict(
                    f"lineage field {field}={target!r} does not reference an "
                    "existing ledger finding"
                )
    if state not in TERMINAL_STATES:
        return
    if not str(merged.get("disposition") or "").strip():
        raise LedgerConflict(
            f"terminal state {state!r} requires a non-empty `disposition`"
        )
    if state == "addressed":
        fix_commit = merged.get("fix_commit")
        has_fix = isinstance(fix_commit, str) and re.fullmatch(
            r"[0-9a-f]{40}", fix_commit
        )
        has_review = bool(str(merged.get("resolution_review_task_id") or "").strip())
        raw_evidence = merged.get("deterministic_evidence")
        if (
            raw_evidence is not None
            and closure_authority_severity(merged) == "critical"
        ):
            raise LedgerConflict(
                "critical findings require review or owner authority, not "
                "deterministic evidence"
            )
        has_evidence = _validate_deterministic_evidence(raw_evidence, existing=existing)
        if raw_evidence is not None and not has_evidence:
            raise LedgerConflict("state 'addressed' has invalid deterministic evidence")
        if not has_fix and not has_review and not has_evidence:
            raise LedgerConflict(
                "state 'addressed' requires `fix_commit` (40-hex) or "
                "`resolution_review_task_id` or valid deterministic evidence — "
                "missing both review/fix provenance and deterministic evidence"
            )
        if fix_commit is not None and not has_fix:
            raise LedgerConflict(
                f"`fix_commit` must be a 40-hex commit SHA, got {fix_commit!r}"
            )
    elif state == "waived":
        for field in ("waiver_decision_record", "waiver_reason"):
            if not str(merged.get(field) or "").strip():
                raise LedgerConflict(f"state 'waived' requires `{field}`")


def _active_leases(primary: Path) -> dict[str, dict[str, object]]:
    active: dict[str, dict[str, object]] = {}
    for record in _read_stream(
        primary / OPERATIONS_DIR,
        require_existing=False,
        stream_name="Finding Ledger operations",
        strict_malformed=True,
    ):
        event_type = record.get("type")
        finding_ids = record.get("finding_ids")
        if event_type == "finding-lease-acquired" and isinstance(finding_ids, list):
            for finding_id in finding_ids:
                if isinstance(finding_id, str):
                    active[finding_id] = record
        if event_type in {
            "finding-lease-released",
            "finding-lease-recovered",
            "terminal-handoff",
        }:
            settled = record.get("settled_lease_ids")
            if not isinstance(settled, list):
                continue
            for finding_id, lease in list(active.items()):
                if lease.get("lease_id") in settled:
                    active.pop(finding_id, None)
    return active


def active_lease(repo: Path | str, finding_id: str) -> dict[str, object] | None:
    """Return the active single-owner lease for an exact finding."""
    return _active_leases(_primary(repo)).get(finding_id)


def active_leases(
    repo: Path | str, finding_ids: Sequence[str]
) -> dict[str, dict[str, object]]:
    """Return active leases for an exact batch with one operation-stream fold."""
    requested = set(finding_ids)
    return {
        finding_id: lease
        for finding_id, lease in _active_leases(_primary(repo)).items()
        if finding_id in requested
    }


def append_finding_transition(
    repo: Path | str,
    finding_id: str,
    updates: Mapping[str, object],
    *,
    operation_id: str,
    expected_state: object,
    expected_disposition: object,
    expected_digest: str,
    lease_owner: Mapping[str, object] | str | None = None,
    required_lease_id: str | None = None,
    allow_promotion_reopen: bool = False,
) -> dict[str, object]:
    """Append one CAS-guarded latest-wins transition."""
    transition_owner = _transition_owner_identity(lease_owner)
    if required_lease_id is not None and not required_lease_id.strip():
        raise LedgerAuthorityError("required lease ID must be non-empty")
    request = {
        "type": "finding-transition",
        "finding_id": finding_id,
        "updates": dict(updates),
        "expected_state": expected_state,
        "expected_disposition": expected_disposition,
        "expected_digest": expected_digest,
        "lease_owner": transition_owner,
        "allow_promotion_reopen": allow_promotion_reopen,
    }
    if required_lease_id is not None:
        request["required_lease_id"] = required_lease_id
    request_digest = _request_digest(request)
    with ledger_lock(repo) as primary:
        replay = _operation_replay(
            primary, operation_id=operation_id, request_digest=request_digest
        )
        if replay is not None:
            return replay
        latest = _latest_by_id(primary, strict_malformed=True)
        existing = latest.get(finding_id)
        if existing is None:
            raise LedgerNotFound(f"finding {finding_id!r} is not in the ledger")
        if existing.get("state") != expected_state:
            raise LedgerConflict(
                f"finding {finding_id!r} expected state {expected_state!r}, "
                f"found {existing.get('state')!r}"
            )
        if existing.get("disposition") != expected_disposition:
            raise LedgerConflict(
                f"finding {finding_id!r} expected disposition "
                f"{expected_disposition!r}, found {existing.get('disposition')!r}"
            )
        actual_digest = canonical_record_digest(existing)
        if actual_digest != expected_digest:
            raise LedgerConflict(
                f"finding {finding_id!r} expected digest {expected_digest}, "
                f"found {actual_digest}"
            )
        promotion_reopen = allow_promotion_reopen and (
            existing.get("advisory") is True
            and updates.get("state") == "open"
            and updates.get("advisory") in {None, False}
        )
        if allow_promotion_reopen and not promotion_reopen:
            raise LedgerAuthorityError(
                "lease bypass is restricted to an advisory-to-formal promotion reopen"
            )
        if promotion_reopen:
            promoted_severity = updates.get("severity", existing.get("severity"))
            if promoted_severity not in _SEVERITY_RANK:
                raise LedgerConflict(f"unsupported severity {promoted_severity!r}")
            existing_rank = _SEVERITY_RANK.get(str(existing.get("severity")), 0)
            if _SEVERITY_RANK[str(promoted_severity)] < existing_rank:
                raise LedgerAuthorityError(
                    "advisory-to-formal promotion cannot lower severity"
                )
            for field in ("original_severity", "severity_correction"):
                if field in updates and updates[field] != existing.get(field):
                    raise LedgerConflict(
                        "promotion does not preserve its assessment lineage"
                    )
        else:
            _validate_transition_severity(existing, updates)
        lease = _active_leases(primary).get(finding_id)
        if not allow_promotion_reopen:
            if required_lease_id is not None and (
                lease is None or lease.get("lease_id") != required_lease_id
            ):
                raise LedgerLeaseConflict(
                    f"finding {finding_id!r} no longer has required active lease "
                    f"{required_lease_id!r}"
                )
            if lease is not None and not _owner_matches(lease, transition_owner):
                raise LedgerLeaseConflict(
                    f"finding {finding_id!r} has an active lease owned by "
                    f"{lease.get('owner')!r}"
                )
        updated = {
            key: value
            for key, value in existing.items()
            if key
            not in {
                "operation_id",
                "operation_request_digest",
                "capture_operation_id",
                "capture_record_kind",
            }
        }
        updated.update(dict(updates))
        updated.update(
            {
                "finding_id": finding_id,
                "operation_id": operation_id,
                "operation_request_digest": request_digest,
                "ts": _timestamp(),
            }
        )
        # Fenced validate+append (#3418 P4): the provenance check and the
        # append share this ledger_lock hold, so two writers cannot both
        # validate against a state neither ends up appending onto.
        _validate_terminal_provenance(updated, updates, latest.keys(), existing)
        _append_jsonl(primary, FINDINGS_DIR, updated)
        return updated


def append_finding_transitions(
    repo: Path | str,
    transitions: Sequence[Mapping[str, object]],
    *,
    operation_id_prefix: str,
    lease_owner: Mapping[str, object] | str | None = None,
) -> list[dict[str, object]]:
    """Validate a transition batch under one lock, then append all or none."""
    if not transitions or not operation_id_prefix.strip():
        raise LedgerConflict(
            "a non-empty transition batch and operation prefix are required"
        )
    owner = _transition_owner_identity(lease_owner)
    ids = [str(item.get("finding_id") or "") for item in transitions]
    if any(not finding_id for finding_id in ids) or len(ids) != len(set(ids)):
        raise LedgerConflict("batch finding IDs must be non-empty and unique")
    with ledger_lock(repo) as primary:
        latest = _latest_by_id(primary, strict_malformed=True)
        leases = _active_leases(primary)
        prepared: list[dict[str, object]] = []
        replays: list[dict[str, object] | None] = []
        for finding_id, transition in zip(ids, transitions, strict=True):
            updates = transition.get("updates")
            if not isinstance(updates, Mapping):
                raise LedgerConflict("batch transition updates must be a mapping")
            request = {
                "type": "finding-transition",
                "finding_id": finding_id,
                "updates": dict(updates),
                "expected_state": transition.get("expected_state"),
                "expected_disposition": transition.get("expected_disposition"),
                "expected_digest": transition.get("expected_digest"),
                "lease_owner": owner,
                "allow_promotion_reopen": False,
            }
            operation_id = f"{operation_id_prefix}:{finding_id}"
            request_digest = _request_digest(request)
            replay = _operation_replay(
                primary, operation_id=operation_id, request_digest=request_digest
            )
            replays.append(replay)
            if replay is not None:
                continue
            existing = latest.get(finding_id)
            if existing is None:
                raise LedgerNotFound(f"finding {finding_id!r} is not in the ledger")
            for field in ("state", "disposition"):
                expected = transition.get(f"expected_{field}")
                if existing.get(field) != expected:
                    raise LedgerConflict(
                        f"finding {finding_id!r} expected {field} {expected!r}, "
                        f"found {existing.get(field)!r}"
                    )
            actual_digest = canonical_record_digest(existing)
            if actual_digest != transition.get("expected_digest"):
                raise LedgerConflict(
                    f"finding {finding_id!r} expected digest "
                    f"{transition.get('expected_digest')}, found {actual_digest}"
                )
            _validate_transition_severity(existing, updates)
            lease = leases.get(finding_id)
            if lease is not None and not _owner_matches(lease, owner):
                raise LedgerLeaseConflict(
                    f"finding {finding_id!r} has an active lease owned by "
                    f"{lease.get('owner')!r}"
                )
            updated = {
                key: value
                for key, value in existing.items()
                if key
                not in {
                    "operation_id",
                    "operation_request_digest",
                    "capture_operation_id",
                    "capture_record_kind",
                }
            }
            updated.update(dict(updates))
            updated.update(
                {
                    "finding_id": finding_id,
                    "operation_id": operation_id,
                    "operation_request_digest": request_digest,
                    "ts": _timestamp(),
                }
            )
            _validate_terminal_provenance(updated, updates, latest.keys(), existing)
            prepared.append(updated)
        if any(replay is not None for replay in replays):
            if not all(replay is not None for replay in replays):
                raise LedgerConflict("batch operation was only partially persisted")
            return [replay for replay in replays if replay is not None]
        for record in prepared:
            _append_jsonl(primary, FINDINGS_DIR, record)
        return prepared


def _append_operation(
    primary: Path,
    *,
    operation_id: str,
    request: Mapping[str, object],
    record: Mapping[str, object],
) -> dict[str, object]:
    request_digest = _request_digest(request)
    replay = _operation_replay(
        primary, operation_id=operation_id, request_digest=request_digest
    )
    if replay is not None:
        return replay
    persisted = {
        **dict(record),
        "operation_id": operation_id,
        "operation_request_digest": request_digest,
        "ts": _timestamp(),
    }
    _append_jsonl(primary, OPERATIONS_DIR, persisted)
    return persisted


def _exact_finding_ids(finding_ids: Sequence[str]) -> list[str]:
    normalized = sorted(set(finding_ids))
    if not normalized or any(not value.strip() for value in normalized):
        raise LedgerConflict("at least one non-empty finding_id is required")
    return normalized


def acquire_finding_lease(
    repo: Path | str,
    *,
    finding_ids: Sequence[str],
    coordinator_id: str,
    run_id: str,
    worker_session_id: str,
    worktree: str,
    branch: str,
    starting_head: str,
    work_kind: str,
    operation_id: str,
) -> dict[str, object]:
    """Acquire one batch lease, rejecting every overlapping active owner."""
    ids = _exact_finding_ids(finding_ids)
    if work_kind not in {"evidence-only", "implementation"}:
        raise LedgerConflict("work_kind must be evidence-only or implementation")
    owner = {
        "coordinator_id": coordinator_id,
        "run_id": run_id,
        "worker_session_id": worker_session_id,
    }
    _require_owner_fields(owner)
    request = {
        "type": "finding-lease-acquired",
        "finding_ids": ids,
        "owner": owner,
        "worktree": worktree,
        "branch": branch,
        "starting_head": starting_head,
        "work_kind": work_kind,
    }
    with ledger_lock(repo) as primary:
        digest = _request_digest(request)
        replay = _operation_replay(
            primary, operation_id=operation_id, request_digest=digest
        )
        if replay is not None:
            return replay
        latest = _latest_by_id(primary, strict_malformed=True)
        missing = [finding_id for finding_id in ids if finding_id not in latest]
        if missing:
            raise LedgerNotFound(
                f"findings are not in the ledger: {', '.join(missing)}"
            )
        active = _active_leases(primary)
        collisions = [finding_id for finding_id in ids if finding_id in active]
        if collisions:
            raise LedgerLeaseConflict(
                f"active finding lease already owns: {', '.join(collisions)}"
            )
        lease_id = "fl_" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:20]
        return _append_operation(
            primary,
            operation_id=operation_id,
            request=request,
            record={
                **request,
                "schema_version": 1,
                "lease_id": lease_id,
            },
        )


def _lease_for_release(
    primary: Path,
    *,
    lease_id: str,
    finding_ids: Sequence[str],
) -> dict[str, object]:
    ids = _exact_finding_ids(finding_ids)
    leases = _active_leases(primary)
    selected = {
        leases[finding_id].get("lease_id") for finding_id in ids if finding_id in leases
    }
    if selected != {lease_id}:
        raise LedgerNotFound(
            f"lease {lease_id!r} is not active for the exact finding set"
        )
    lease = next(
        (leases[finding_id] for finding_id in ids if finding_id in leases),
        None,
    )
    if lease is None:
        raise LedgerNotFound(f"lease {lease_id!r} is not active")
    leased_ids = lease.get("finding_ids")
    if (
        not isinstance(leased_ids, list)
        or sorted(value for value in leased_ids if isinstance(value, str)) != ids
    ):
        raise LedgerConflict("finding lease must be released as its exact batch")
    return lease


def release_finding_lease(
    repo: Path | str,
    *,
    lease_id: str,
    finding_ids: Sequence[str],
    operation_id: str,
    lease_owner: Mapping[str, object],
    reason: str,
) -> dict[str, object]:
    """Explicitly release a lease by its current owner."""
    if not isinstance(lease_owner, Mapping):
        raise LedgerAuthorityError("lease release requires a full owner Mapping")
    try:
        _require_owner_fields(lease_owner)
    except LedgerConflict as exc:
        raise LedgerAuthorityError(
            "lease release requires a full owner Mapping"
        ) from exc
    request = {
        "type": "finding-lease-released",
        "lease_id": lease_id,
        "finding_ids": _exact_finding_ids(finding_ids),
        "owner": _owner_identity(lease_owner),
        "reason": reason,
    }
    with ledger_lock(repo) as primary:
        digest = _request_digest(request)
        replay = _operation_replay(
            primary, operation_id=operation_id, request_digest=digest
        )
        if replay is not None:
            return replay
        lease = _lease_for_release(primary, lease_id=lease_id, finding_ids=finding_ids)
        if not _owner_matches(lease, lease_owner):
            raise LedgerAuthorityError("only the exact lease owner may release it")
        return _append_operation(
            primary,
            operation_id=operation_id,
            request=request,
            record={
                **request,
                "schema_version": 1,
                "settled_lease_ids": [lease_id],
            },
        )


def _validate_recovery_proof(proof: Mapping[str, object]) -> dict[str, object]:
    if proof.get("owner_gone") is not True:
        raise LedgerAuthorityError("audited recovery requires owner_gone=true")
    evidence = proof.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise LedgerAuthorityError(
            "audited recovery requires machine-checkable evidence"
        )
    required_sources = {"worktree_guard", "delivery_liveness", "worker_session"}
    observed_sources: set[str] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            raise LedgerAuthorityError(
                "audited recovery evidence items must be objects"
            )
        source = item.get("source")
        status = item.get("status")
        reference = item.get("reference")
        if source not in required_sources:
            raise LedgerAuthorityError(
                "audited recovery evidence has an unknown source"
            )
        if source in observed_sources:
            raise LedgerAuthorityError("audited recovery evidence repeats a source")
        if status != "gone" or not isinstance(reference, str) or not reference.strip():
            raise LedgerAuthorityError(
                "audited recovery requires a machine-checkable gone status and reference"
            )
        observed_sources.add(str(source))
    missing = sorted(required_sources - observed_sources)
    if missing:
        raise LedgerAuthorityError(
            "audited recovery evidence is incomplete: " + ", ".join(missing)
        )
    return dict(proof)


def _finding_lease_state(primary: Path, lease_id: str) -> str | None:
    state: str | None = None
    for record in load_operation_history(primary):
        if record.get("lease_id") != lease_id:
            continue
        event_type = record.get("type")
        if event_type == "finding-lease-acquired":
            state = "eligible"
        elif event_type == "finding-lease-owner-terminal":
            state = "owner-terminal"
        elif event_type in {
            "finding-lease-released",
            "finding-lease-recovered",
        }:
            state = "settled"
    return state


def mark_finding_lease_owner_terminal(
    repo: Path | str,
    *,
    lease_id: str,
    finding_ids: Sequence[str],
    operation_id: str,
    terminal_evidence: Mapping[str, object],
) -> dict[str, object]:
    """Record that a lease owner is terminal without silently releasing it."""
    if terminal_evidence.get("terminal") is not True:
        raise LedgerAuthorityError("terminal finding-lease evidence must be terminal")
    request = {
        "type": "finding-lease-owner-terminal",
        "lease_id": lease_id,
        "finding_ids": _exact_finding_ids(finding_ids),
        "terminal_evidence": dict(terminal_evidence),
    }
    with ledger_lock(repo) as primary:
        digest = _request_digest(request)
        replay = _operation_replay(
            primary, operation_id=operation_id, request_digest=digest
        )
        if replay is not None:
            return replay
        _lease_for_release(primary, lease_id=lease_id, finding_ids=finding_ids)
        return _append_operation(
            primary,
            operation_id=operation_id,
            request=request,
            record={**request, "schema_version": 1},
        )


def recover_finding_lease(
    repo: Path | str,
    *,
    lease_id: str,
    finding_ids: Sequence[str],
    operation_id: str,
    recovery_proof: Mapping[str, object],
) -> dict[str, object]:
    """Release an abandoned lease only with durable owner-gone evidence."""
    proof = _validate_recovery_proof(recovery_proof)
    request = {
        "type": "finding-lease-recovered",
        "lease_id": lease_id,
        "finding_ids": _exact_finding_ids(finding_ids),
        "recovery_proof": proof,
    }
    with ledger_lock(repo) as primary:
        digest = _request_digest(request)
        replay = _operation_replay(
            primary, operation_id=operation_id, request_digest=digest
        )
        if replay is not None:
            return replay
        _lease_for_release(primary, lease_id=lease_id, finding_ids=finding_ids)
        if _finding_lease_state(primary, lease_id) != "owner-terminal":
            raise LedgerAuthorityError(
                "finding lease recovery requires an owner-terminal marker"
            )
        return _append_operation(
            primary,
            operation_id=operation_id,
            request=request,
            record={
                **request,
                "schema_version": 1,
                "settled_lease_ids": [lease_id],
            },
        )


def _accepted_evidence(evidence: Sequence[Mapping[str, object]]) -> bool:
    return bool(evidence) and all(item.get("accepted") is True for item in evidence)


def record_terminal_handoff(
    repo: Path | str,
    *,
    operation_id: str,
    mode: str,
    finding_ids: Sequence[str],
    terminal: bool,
    terminal_session_id: str,
    terminal_run_id: str,
    branch: str,
    starting_head: str,
    final_head: str,
    worktree: str,
    worktree_clean: bool,
    commits: Sequence[str],
    pr_number: int | None,
    merge_commit: str | None,
    primary_contains_merge: bool,
    validations: Sequence[Mapping[str, object]],
    accepted_review_evidence: Sequence[Mapping[str, object]],
    unresolved_findings: Sequence[str],
    blockers: Sequence[str],
    lease_owner: Mapping[str, object] | None = None,
    settle_leases: bool = False,
    outcome: str = "success",
) -> dict[str, object]:
    """Record deterministic worker evidence and optionally settle its leases."""
    if mode not in {"evidence-only", "implementation"}:
        raise LedgerConflict("handoff mode must be evidence-only or implementation")
    if outcome not in {"success", "blocked", "failed"}:
        raise LedgerConflict("handoff outcome must be success, blocked, or failed")
    ids = _exact_finding_ids(finding_ids)
    terminal_owner = _transition_owner_identity(lease_owner)
    request = {
        "type": "terminal-handoff",
        "mode": mode,
        "finding_ids": ids,
        "terminal": terminal,
        "terminal_session_id": terminal_session_id,
        "terminal_run_id": terminal_run_id,
        "branch": branch,
        "starting_head": starting_head,
        "final_head": final_head,
        "worktree": worktree,
        "worktree_clean": worktree_clean,
        "commits": list(commits),
        "pr_number": pr_number,
        "merge_commit": merge_commit,
        "primary_contains_merge": primary_contains_merge,
        "validations": [dict(value) for value in validations],
        "accepted_review_evidence": [dict(value) for value in accepted_review_evidence],
        "unresolved_findings": sorted(set(unresolved_findings)),
        "blockers": list(blockers),
        "owner": terminal_owner,
        "settle_leases": settle_leases,
        "outcome": outcome,
    }
    with ledger_lock(repo) as primary:
        digest = _request_digest(request)
        replay = _operation_replay(
            primary, operation_id=operation_id, request_digest=digest
        )
        if replay is not None:
            return replay
        settled: list[str] = []
        if settle_leases:
            if terminal_owner is None:
                raise LedgerAuthorityError(
                    "terminal handoff settlement requires a full owner Mapping"
                )
            if not terminal:
                raise LedgerAuthorityError(
                    "a non-terminal handoff cannot settle a lease"
                )
            active = _active_leases(primary)
            leases: dict[str, dict[str, object]] = {}
            for finding_id in ids:
                lease = active.get(finding_id)
                if lease is None:
                    raise LedgerNotFound(f"finding {finding_id!r} has no active lease")
                leases[str(lease["lease_id"])] = lease
            for lease in leases.values():
                if not _owner_matches(lease, terminal_owner):
                    raise LedgerAuthorityError("handoff does not own the active lease")
                lease_owner_record = lease.get("owner")
                if not isinstance(lease_owner_record, Mapping) or (
                    terminal_session_id != lease_owner_record.get("worker_session_id")
                    or terminal_run_id != lease_owner_record.get("run_id")
                    or branch != lease.get("branch")
                    or worktree != lease.get("worktree")
                    or starting_head != lease.get("starting_head")
                ):
                    raise LedgerAuthorityError(
                        "handoff identity does not match the active lease"
                    )
                leased_ids = lease.get("finding_ids")
                if not isinstance(leased_ids, list) or any(
                    finding_id not in ids for finding_id in leased_ids
                ):
                    raise LedgerAuthorityError(
                        "handoff must settle each finding lease as its exact batch"
                    )
                if (
                    lease.get("work_kind") == "implementation"
                    and mode == "evidence-only"
                ):
                    raise LedgerAuthorityError(
                        "an evidence-only handoff cannot settle an implementation lease"
                    )
            if mode == "implementation" and outcome == "success":
                if not (
                    worktree_clean
                    and pr_number is not None
                    and merge_commit
                    and primary_contains_merge
                    and _accepted_evidence(accepted_review_evidence)
                    and not unresolved_findings
                    and not blockers
                ):
                    raise LedgerAuthorityError(
                        "successful implementation handoff requires a clean merged PR "
                        "on primary, accepted review, and no unresolved blockers"
                    )
            settled = sorted(leases)
        return _append_operation(
            primary,
            operation_id=operation_id,
            request=request,
            record={
                **request,
                "schema_version": 1,
                "settled_lease_ids": settled,
            },
        )


def _active_campaign_drains(primary: Path) -> dict[str, dict[str, object]]:
    active: dict[str, dict[str, object]] = {}
    for record in _read_stream(
        primary / OPERATIONS_DIR,
        require_existing=False,
        stream_name="Finding Ledger operations",
        strict_malformed=True,
    ):
        campaign_id = record.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            continue
        if record.get("type") == "campaign-drain-activated":
            active[campaign_id] = record
        elif record.get("type") == "campaign-drain-cleared":
            active.pop(campaign_id, None)
    return active


# Audit-campaign drain policy stays in the consumer.


def _is_security_path(record: Mapping[str, object]) -> bool:
    anchor = record.get("anchor")
    path = str(anchor.get("path", "")) if isinstance(anchor, Mapping) else ""
    return bool(path) and any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(f"/{path}", pattern)
        for pattern in _SECURITY_PATH_PATTERNS
    )

def closure_authority_severity(record: Mapping[str, object]) -> str:
    """Return the stricter of original and corrected severity."""
    current = str(record.get("severity") or "suggestion")
    original = str(record.get("original_severity") or current)
    return max((current, original), key=lambda value: _SEVERITY_RANK.get(value, 0))


def append_severity_correction(
    repo: Path | str,
    *,
    finding_id: str,
    new_severity: str,
    reason: str,
    decision_record: str | None,
    reviewer_task_id: str | None,
    owner_approved: bool,
    operation_id: str,
    expected_state: object,
    expected_disposition: object,
    expected_digest: str,
    lease_owner: Mapping[str, object] | str | None = None,
) -> dict[str, object]:
    """Append a governed severity correction without erasing the assessment."""
    if new_severity not in _SEVERITY_RANK:
        raise LedgerConflict(f"unsupported severity {new_severity!r}")
    finding_history = [
        record
        for record in load_finding_history(repo, strict_malformed=True)
        if record.get("finding_id") == finding_id
    ]
    if not finding_history:
        raise LedgerNotFound(f"finding {finding_id!r} is not in the ledger")
    matching = [
        record
        for record in finding_history
        if canonical_record_digest(record) == expected_digest
    ]
    if not matching:
        raise LedgerConflict(
            f"finding {finding_id!r} has no record with expected digest {expected_digest}"
        )
    observed = matching[-1]
    authority = closure_authority_severity(observed)
    requires_decision = _SEVERITY_RANK.get(authority, 0) >= _SEVERITY_RANK["important"]
    if requires_decision and not decision_record:
        raise LedgerAuthorityError(
            "important severity corrections require a Decision Record"
        )
    if (authority == "critical" or _is_security_path(observed)) and not owner_approved:
        raise LedgerAuthorityError(
            "critical or security-path severity corrections require owner authority"
        )
    original = str(observed.get("original_severity") or observed.get("severity"))
    correction = {
        "from": observed.get("severity"),
        "to": new_severity,
        "reason": reason,
        "decision_record": decision_record,
        "reviewer_task_id": reviewer_task_id,
        "owner_approved": owner_approved,
    }
    return append_finding_transition(
        repo,
        finding_id,
        {
            "severity": new_severity,
            "original_severity": original,
            "severity_correction": correction,
        },
        operation_id=operation_id,
        expected_state=expected_state,
        expected_disposition=expected_disposition,
        expected_digest=expected_digest,
        lease_owner=lease_owner,
    )


def append_reviewed_lineage(
    repo: Path | str,
    *,
    finding_ids: Sequence[str],
    review_task_id: str,
    accepted_review_evidence: Mapping[str, object],
    operation_id: str,
) -> dict[str, object]:
    """Relate distinct evidence IDs only with explicit accepted review."""
    ids = _exact_finding_ids(finding_ids)
    if len(ids) < 2:
        raise LedgerConflict("same_defect_as lineage requires at least two findings")
    if (
        accepted_review_evidence.get("accepted") is not True
        or accepted_review_evidence.get("task_id") != review_task_id
    ):
        raise LedgerAuthorityError("lineage requires matching accepted review evidence")
    request = {
        "type": "finding-lineage",
        "relation": "same_defect_as",
        "finding_ids": ids,
        "review_task_id": review_task_id,
        "accepted_review_evidence": dict(accepted_review_evidence),
    }
    with ledger_lock(repo) as primary:
        latest = _latest_by_id(primary)
        missing = [finding_id for finding_id in ids if finding_id not in latest]
        if missing:
            raise LedgerNotFound(
                f"findings are not in the ledger: {', '.join(missing)}"
            )
        return _append_operation(
            primary,
            operation_id=operation_id,
            request=request,
            record={**request, "schema_version": 1},
        )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _underlying_count(primary: Path, finding_ids: set[str]) -> int:
    parent = {finding_id: finding_id for finding_id in finding_ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for record in _read_stream(
        primary / OPERATIONS_DIR,
        require_existing=False,
        stream_name="Finding Ledger operations",
        strict_malformed=True,
    ):
        if record.get("type") != "finding-lineage":
            continue
        record_ids = record.get("finding_ids")
        if not isinstance(record_ids, list):
            continue
        ids = [
            value for value in record_ids if isinstance(value, str) and value in parent
        ]
        for value in ids[1:]:
            union(ids[0], value)
    return len({find(value) for value in parent})


# Audit-campaign metrics stay in the consumer.

def scan_terminal_provenance(repo: Path | str) -> dict[str, object]:
    """Read-only compatibility scan for the P4 provenance-at-write contract.

    Reports how the CURRENT latest records fare against the enforcement that
    now guards new transitions. Historical records are never re-validated at
    write time (enforcement is forward-only, per the direct-cutover rule);
    this scan is the evidence that active producers already satisfy the
    contract, plus a torn-tail check on the newest shard.
    """
    primary = _primary(repo)
    # Torn-tail detection first: a crashed writer's partial final line makes
    # strict reads fail closed, so the scan must not itself require a strict
    # load to report the very condition it exists to surface.
    torn: list[str] = []
    directory = primary / FINDINGS_DIR
    if directory.is_dir():
        shards = sorted(directory.glob("*.jsonl"))
        if shards:
            raw = shards[-1].read_bytes()
            if raw and not raw.endswith(b"\n"):
                torn.append(str(shards[-1]))
    latest: dict[str, dict[str, object]] = {}
    for record in _read_stream(
        primary / FINDINGS_DIR,
        require_existing=False,
        stream_name="Finding Ledger",
        strict_malformed=False,
    ):
        finding_id = record.get("finding_id")
        if isinstance(finding_id, str) and finding_id:
            latest[finding_id] = record
    known = set(latest.keys())
    violations: list[dict[str, str]] = []
    for finding_id, record in latest.items():
        try:
            _validate_terminal_provenance(record, record, known, None)
        except LedgerConflict as exc:
            violations.append({"finding_id": finding_id, "error": str(exc)})
    return {
        "records": len(latest),
        "violations": violations,
        "torn_tail_shards": torn,
    }


# Stale-suggestion retention stays in the consumer.

# CLI composition stays in the consumer.
