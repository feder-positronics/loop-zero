#!/usr/bin/env python3
"""Review snapshots, evidence staging, and finding capture (#3944 decomposition).

Owns the immutable review-snapshot lifecycle (create/cleanup/record), evidence
snapshot staging, verification, mirroring and sweeps, the merge-delta path
manifest, and Finding Ledger capture of review results. Extracted move-only
from `agent_dispatch.py`; the facade re-exports every moved name. This module
must not import `agent_dispatch`.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from contextvars import ContextVar

from ..config import Profile
from ..runners.contract import FINDING_SEVERITIES
from ..runners.contract import MAX_RESULT_ITEMS
from ..runners.contract import MAX_RESULT_PATH
from ..runners.contract import MAX_RESULT_STRING
from ..kernel import patch_identity
from ..kernel.gitscope import (
    _deterministic_identifier_sha256,
    DispatchError,
    primary_repo_root,
    ReviewSnapshot,
    trusted_git_command,
)
from .findings import build_finding_capture_request
from .findings import capture_finding_records as capture_finding_batch
from .findings import LedgerConflict
from .findings import replay_finding_capture
from .routing import REVIEW_INTENTS
from ..kernel.sandbox import environment as sandbox_environment
from ..kernel.worktree_lease import worktree_lease

_PROFILE: ContextVar[Profile | None] = ContextVar("review_evidence_profile", default=None)
_DEFAULT_PROFILE: Profile | None = None


def configure(profile: Profile) -> None:
    global _DEFAULT_PROFILE
    _DEFAULT_PROFILE = profile
    _PROFILE.set(profile)


def _profile() -> Profile | None:
    return _PROFILE.get() or _DEFAULT_PROFILE


def _audit_root(repo: Path) -> Path:
    profile = _profile()
    if profile is None:
        raise DispatchError("review evidence requires a configured profile")
    return repo.resolve() / profile.audit_root


def _ref_namespaces() -> tuple[str, str]:
    profile = _profile()
    if profile is None:
        raise DispatchError("review evidence requires a configured profile")
    return (
        getattr(profile, "review_snapshot_namespace", "dispatch-snapshots"),
        getattr(profile, "finding_snapshot_namespace", "finding-snapshots"),
    )

PERSISTED_REVIEW_SEVERITIES = frozenset({"critical", "important"})
# Every review intent deposits material findings except the trust-manifest
# verifier, whose verdict stays in the terminal record; derive the allowlist so
# a new intent cannot silently drop critical findings from the ledger.
PERSISTED_REVIEW_INTENTS = frozenset(REVIEW_INTENTS) - {"trust-manifest-verification"}

EVIDENCE_SNAPSHOT_SCHEMA_VERSION = "dispatch-evidence-v1"

MERGE_DELTA_PATH_MANIFEST_SCHEMA_VERSION = "delivery-merge-delta-paths-v1"

EVIDENCE_MAX_FILES = 64

EVIDENCE_MAX_FILE_BYTES = 5 * 1024 * 1024

EVIDENCE_MAX_TOTAL_BYTES = 20 * 1024 * 1024

EVIDENCE_OWNER_FILENAME = ".dispatch-evidence-owner.json"

EVIDENCE_MANIFEST_FILENAME = "manifest.json"

EVIDENCE_STALE_AFTER = timedelta(hours=24)

@dataclass(frozen=True)
class EvidenceSnapshot:
    directory: Path
    files: tuple[Path, ...]
    manifest: tuple[dict[str, object], ...]
    task_id: str


class EvidenceScopeTooLargeError(DispatchError):
    """A mechanically expanded trust evidence scope exceeded its hard cap."""

    code = "evidence-scope-too-large"

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"{self.code}: expanded evidence contains {count} paths "
            f"(limit {EVIDENCE_MAX_FILES})"
        )


def _validate_result_findings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_RESULT_ITEMS:
        raise DispatchError("worker result findings are invalid")
    for finding in value:
        if not isinstance(finding, dict) or not {"severity", "claim"} <= set(finding):
            raise DispatchError("worker result finding entry is invalid")
        if set(finding) - {"severity", "claim", "path", "line_start", "line_end"}:
            raise DispatchError("worker result finding entry is invalid")
        if finding["severity"] not in FINDING_SEVERITIES:
            raise DispatchError("worker result finding severity is invalid")
        claim = finding["claim"]
        if not isinstance(claim, str) or not claim or len(claim) > MAX_RESULT_STRING:
            raise DispatchError("worker result finding claim is invalid")
        # Null is the strict-schema representation of an omitted optional anchor.
        path = finding.get("path")
        if path is not None:
            if not isinstance(path, str) or len(path) > MAX_RESULT_PATH:
                raise DispatchError("worker result finding path is invalid")
        spans = [finding.get("line_start"), finding.get("line_end")]
        for span in spans:
            if span is not None and (
                isinstance(span, bool) or not isinstance(span, int) or span < 1
            ):
                raise DispatchError("worker result finding line span is invalid")
        if any(span is not None for span in spans) and path is None:
            raise DispatchError("worker result finding span requires a path")
        if spans[0] is not None and spans[1] is not None and spans[1] < spans[0]:
            raise DispatchError("worker result finding line span is inverted")
    return value

def _evidence_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def cleanup_evidence_snapshot(snapshot: EvidenceSnapshot) -> None:
    """Remove one dispatcher-owned snapshot and no neighboring content."""
    shutil.rmtree(snapshot.directory)

def _review_snapshot_task_token(task_id: str) -> str:
    """Return the exact, deterministic task binding for a snapshot checkout."""
    return _deterministic_identifier_sha256(task_id)

_SNAPSHOT_IDENT_ENV = {
    "GIT_AUTHOR_NAME": "agent-dispatch",
    "GIT_AUTHOR_EMAIL": "dispatch@local",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "agent-dispatch",
    "GIT_COMMITTER_EMAIL": "dispatch@local",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
}

_SNAPSHOT_GIT_OVERRIDE_KEYS = frozenset({"GIT_INDEX_FILE", *_SNAPSHOT_IDENT_ENV})

def _snapshot_git(
    worktree: Path, *args: str, env: Mapping[str, str] | None = None
) -> str:
    overrides = dict(env or {})
    if unexpected := set(overrides) - _SNAPSHOT_GIT_OVERRIDE_KEYS:
        raise DispatchError(
            "review-snapshot Git environment override is invalid: "
            + ", ".join(sorted(unexpected))
        )
    environment = {**sandbox_environment(os.environ), **overrides}
    completed = subprocess.run(
        trusted_git_command(worktree, *args),
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise DispatchError(
            f"review-snapshot git {args[0]} failed: {completed.stderr.strip()[:200]}"
        )
    return completed.stdout.strip()

def create_review_snapshot(
    worktree: Path,
    task_id: str,
    *,
    ref_namespace: str | None = None,
    retain_ref: bool = True,
) -> ReviewSnapshot:
    """Materialize the exact reviewed state as a content-addressed commit.

    Binds review evidence to an immutable tree (#3106 A2, decision FL-1):
    live-tree drift after dispatch can no longer void the review. The
    reviewer runs in an ephemeral worktree of the snapshot commit; the
    `refs/<namespace>/<task>` ref normally preserves provenance after the
    checkout is removed; read-only working-tree classification may opt out of
    retaining a ref. Formal dispatches and local finding captures use separate
    closed namespaces so a local review ID cannot overwrite publication
    provenance. `git add -A` honors the consumer's ignore rules, so private
    state and environment files never enter the snapshot.
    """
    review_namespace, finding_namespace = _ref_namespaces()
    ref_namespace = ref_namespace or review_namespace
    if ref_namespace not in {review_namespace, finding_namespace}:
        raise DispatchError("unsupported review snapshot namespace")
    resolved = worktree.resolve()
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    task_token = _review_snapshot_task_token(task_id)
    head = _snapshot_git(resolved, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="dispatch-snapshot-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _snapshot_git(resolved, "read-tree", "HEAD", env=env)
        _snapshot_git(resolved, "add", "-A", env=env)
        tree_sha = _snapshot_git(resolved, "write-tree", env=env)
    # Fixed message + fixed ident: identical content yields an identical
    # commit SHA regardless of task, so re-snapshots are idempotent. Task
    # provenance lives in the ref name, not the commit.
    commit_sha = _snapshot_git(
        resolved,
        "commit-tree",
        tree_sha,
        "-p",
        head,
        "-m",
        "dispatch-snapshot",
        env=_SNAPSHOT_IDENT_ENV,
    )
    ref_suffix = (
        safe_task_id
        if ref_namespace == review_namespace
        else f"{safe_task_id}-{tree_sha[:12]}"
    )
    if retain_ref:
        _snapshot_git(
            resolved,
            "update-ref",
            f"refs/{ref_namespace}/{ref_suffix}",
            commit_sha,
        )
    directory = Path(
        tempfile.mkdtemp(prefix=f"dispatch-snapshot-{safe_task_id}-{task_token}-")
    )
    try:
        _snapshot_git(
            resolved, "worktree", "add", "--detach", str(directory), commit_sha
        )
    except DispatchError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    head_tree = _snapshot_git(resolved, "rev-parse", f"{head}^{{tree}}")
    identity_candidate = head if head_tree == tree_sha else commit_sha
    identity = patch_identity.capture_patch_identity(
        resolved, candidate_sha=identity_candidate
    )
    return ReviewSnapshot(commit_sha, tree_sha, directory, identity)

def _finding_anchor(
    worktree: Path,
    snapshot: ReviewSnapshot | None,
    finding: dict[str, object],
) -> dict[str, object] | None:
    """Compute the durable content anchor for one finding (#3106 A2 L2)."""
    path = finding.get("path")
    if not isinstance(path, str) or not path or snapshot is None:
        return None
    resolved = worktree.resolve()
    blob = subprocess.run(
        trusted_git_command(resolved, "rev-parse", f"{snapshot.commit_sha}:{path}"),
        capture_output=True,
        text=True,
        env=sandbox_environment(os.environ),
    )
    if blob.returncode != 0:
        return None
    anchor: dict[str, object] = {"path": path, "blob_sha": blob.stdout.strip()}
    start = finding.get("line_start")
    end = finding.get("line_end")
    if isinstance(start, int) and not isinstance(start, bool):
        span_end = end if isinstance(end, int) and not isinstance(end, bool) else start
        anchor["line_span"] = [start, span_end]
        content = subprocess.run(
            trusted_git_command(resolved, "show", f"{snapshot.commit_sha}:{path}"),
            capture_output=True,
            text=True,
            env=sandbox_environment(os.environ),
        )
        if content.returncode == 0:
            lines = content.stdout.splitlines()
            context = lines[max(0, start - 4) : span_end + 3]
            anchor["hunk_context_sha"] = hashlib.sha256(
                "\n".join(context).encode()
            ).hexdigest()
    return anchor

def _finding_capture_id(content_identity: str, finding: Mapping[str, object]) -> str:
    span = (finding.get("line_start"), finding.get("line_end"))
    identity_payload = "|".join(
        (
            content_identity,
            str(finding.get("path") or ""),
            f"{span[0]}-{span[1]}",
            str(finding["claim"]),
        )
    )
    return "f_" + hashlib.sha256(identity_payload.encode()).hexdigest()[:20]

def persisted_review_findings(
    findings: list[dict[str, object]], *, review_intent: str,
) -> list[dict[str, object]]:
    """Select ledger intake without changing the complete review evidence."""
    if review_intent not in PERSISTED_REVIEW_INTENTS:
        return []
    return [
        finding for finding in findings
        if finding.get("severity") in PERSISTED_REVIEW_SEVERITIES
    ]


def append_finding_records(
    repo: Path,
    *,
    task_id: str,
    result: dict[str, object],
    snapshot: ReviewSnapshot | None,
    worktree: Path,
    advisory: bool,
    source_head: str | None,
    producer_skill: str | None = None,
    category: str | None = None,
    producer_kind: str = "governed-review",
    unit_attempt_number: int | None = 1,
    review_intent: str = "discovery",
    delivery_run_id: str | None = None,
    pr: int,
) -> dict[str, object] | None:
    """Atomically deposit one producer result, retaining content identity.

    `finding_id` derives from the reviewed source identity (snapshot tree or
    source head), the anchor, and the claim. Producer-operation replay is
    resolved before that source identity is consulted.
    """
    if review_intent == "trust-manifest-verification":
        from .authority import normalize_review_result

        normalize_review_result(result, task={"review_intent": review_intent})
    findings = result.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    validated = _validate_result_findings(findings)
    if producer_kind == "governed-review":
        # Intake policy lives here so dispatch and recovery share it. Keep the
        # complete result envelope unchanged for receipts and PR visibility.
        validated = persisted_review_findings(validated, review_intent=review_intent)
        if not validated:
            return None
    resolved_skill = producer_skill or producer_kind
    resolved_category = category or "uncategorized"
    request = build_finding_capture_request(
        pr=pr,
        producer_kind=producer_kind,
        producer_id=task_id,
        unit_attempt_number=unit_attempt_number,
        producer_skill=resolved_skill,
        category=resolved_category,
        advisory=advisory,
        findings=validated,
    )
    run_binding: dict[str, object] = {}
    if delivery_run_id:
        from ..kernel.run_log import load_entries
        from ..kernel.run_identity import run_delivery_contract
        from .findings import authenticated_delivery_run_pr

        entries = load_entries(_audit_root(repo) / "skill-runs")
        try:
            contract = run_delivery_contract(entries, delivery_run_id)
            bound_pr = authenticated_delivery_run_pr(repo, delivery_run_id)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LedgerConflict(
                "delivery run has no authenticated loop-zero PR binding"
            ) from exc
        if contract != "loop-zero-v1":
            raise LedgerConflict("finding capture requires a loop-zero delivery run")
        if bound_pr != pr:
            raise LedgerConflict(
                f"delivery run is authenticated for PR {bound_pr}, not PR {pr}"
            )
        run_binding["delivery_run_id"] = delivery_run_id
    replay = replay_finding_capture(repo, request=request)
    if replay is not None:
        return replay
    content_identity = (
        snapshot.tree_sha if snapshot is not None else (source_head or "")
    )
    candidates: list[dict[str, object]] = []
    for finding in validated:
        claim = str(finding["claim"])
        candidates.append(
            {
                **run_binding,
                "pr": pr,
                "finding_id": _finding_capture_id(content_identity, finding),
                "review_task_id": task_id,
                "producer_skill": resolved_skill,
                "category": resolved_category,
                "snapshot_sha": snapshot.commit_sha if snapshot else None,
                "snapshot_tree_sha": snapshot.tree_sha if snapshot else None,
                "patch_identity": snapshot.patch_identity if snapshot else None,
                "severity": finding["severity"],
                "claim": claim,
                "anchor": _finding_anchor(worktree, snapshot, finding),
                "advisory": advisory or None,
                "state": "open",
                "disposition": None,
            }
        )
    return capture_finding_batch(
        repo,
        request=request,
        candidate_records=candidates,
        source_identity={
            "snapshot_sha": snapshot.commit_sha if snapshot else None,
            "snapshot_tree_sha": snapshot.tree_sha if snapshot else None,
            "source_head": source_head,
            "patch_identity": snapshot.patch_identity if snapshot else None,
        },
    )

def record_local_findings(
    *,
    worktree: Path,
    review_id: str,
    producer_skill: str,
    category: str,
    findings: list[dict[str, object]],
    pr: int,
) -> dict[str, object]:
    """Anchor and deposit findings from an in-process or local review.

    Local producers share the Finding Ledger with governed dispatches, but
    their records are always advisory: durable evidence, never publication
    evidence. Every local claim must anchor to the captured source tree so a
    narrative-only or stale path cannot enter the ledger.
    """
    if not review_id.strip() or not producer_skill.strip() or not category.strip():
        raise DispatchError("local finding capture requires non-empty identity fields")
    validated = _validate_result_findings(findings)
    if not validated:
        raise DispatchError("local finding capture requires at least one finding")
    resolved = worktree.resolve()
    repo = primary_repo_root(resolved)
    request = build_finding_capture_request(
        pr=pr,
        producer_kind="local-review",
        producer_id=review_id,
        unit_attempt_number=None,
        producer_skill=producer_skill,
        category=category,
        advisory=True,
        findings=validated,
    )
    replay = replay_finding_capture(repo, request=request)
    if replay is not None:
        return replay
    snapshot: ReviewSnapshot | None = None
    with worktree_lease(
        resolved,
        boundary=f"local-finding-capture:{review_id}",
        timeout_s=300.0,
    ):
        snapshot = create_review_snapshot(
            resolved, review_id, ref_namespace=_ref_namespaces()[1]
        )
        try:
            for finding in validated:
                if _finding_anchor(resolved, snapshot, finding) is None:
                    raise DispatchError(
                        "every local finding must anchor to a path in the captured "
                        "source tree"
                    )
            return append_finding_records(
                repo,
                task_id=review_id,
                result={"findings": validated},
                snapshot=snapshot,
                worktree=resolved,
                advisory=True,
                source_head=None,
                producer_skill=producer_skill,
                category=category,
                producer_kind="local-review",
                unit_attempt_number=None,
                pr=pr,
            )
        finally:
            cleanup_review_snapshot(resolved, snapshot)

def cleanup_review_snapshot(worktree: Path, snapshot: ReviewSnapshot) -> None:
    """Remove the ephemeral checkout; the snapshot ref stays as provenance."""
    subprocess.run(
        trusted_git_command(
            worktree.resolve(),
            "worktree",
            "remove",
            "--force",
            str(snapshot.directory),
        ),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    shutil.rmtree(snapshot.directory, ignore_errors=True)

def _is_valid_git_sha(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))

def recorded_review_snapshot(
    start: dict[str, object], repo: Path
) -> ReviewSnapshot | None:
    """Return a marker-validated snapshot owned by one review attempt.

    Attempt-start telemetry is untrusted input when an operator later aborts a
    killed worker.  The directory name, registered checkout, commit, and tree
    must all agree before cleanup is allowed; otherwise an abort only records
    its terminal state.
    """
    if start.get("work_kind") != "review":
        return None
    task_id = start.get("task_id")
    commit_sha = start.get("snapshot_sha")
    tree_sha = start.get("snapshot_tree_sha")
    directory_value = start.get("snapshot_worktree")
    if (
        not isinstance(task_id, str)
        or not _is_valid_git_sha(commit_sha)
        or not _is_valid_git_sha(tree_sha)
        or not isinstance(directory_value, str)
        or not directory_value
    ):
        return None
    directory = Path(directory_value)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        return None
    resolved = directory.resolve()
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    task_token = _review_snapshot_task_token(task_id)
    if resolved.parent != Path(
        tempfile.gettempdir()
    ).resolve() or not resolved.name.startswith(
        f"dispatch-snapshot-{safe_task_id}-{task_token}-"
    ):
        return None
    worktrees = subprocess.run(
        trusted_git_command(repo.resolve(), "worktree", "list", "--porcelain"),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if worktrees.returncode != 0:
        return None
    registered = False
    for block in worktrees.stdout.split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        recorded_path = fields.get("worktree")
        if (
            isinstance(recorded_path, str)
            and Path(recorded_path).resolve() == resolved
            and fields.get("HEAD") == commit_sha
        ):
            registered = True
            break
    if not registered:
        return None
    tree = subprocess.run(
        trusted_git_command(repo.resolve(), "rev-parse", f"{commit_sha}^{{tree}}"),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if tree.returncode != 0 or tree.stdout.strip() != tree_sha:
        return None
    recorded_identity = start.get("patch_identity")
    identity = (
        dict(recorded_identity) if isinstance(recorded_identity, Mapping) else None
    )
    return ReviewSnapshot(commit_sha, tree_sha, resolved, identity)

def dispatcher_snapshot_worktrees(repo: Path) -> list[Path]:
    """List existing dispatcher review checkouts without exposing git output."""
    completed = subprocess.run(
        trusted_git_command(repo.resolve(), "worktree", "list", "--porcelain"),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode != 0:
        return []
    snapshots: list[Path] = []
    for block in completed.stdout.split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        value = fields.get("worktree")
        if not isinstance(value, str):
            continue
        directory = Path(value)
        if (
            directory.is_absolute()
            and directory.parent == Path(tempfile.gettempdir()).resolve()
            and directory.name.startswith("dispatch-snapshot-")
            and not directory.is_symlink()
            and directory.is_dir()
        ):
            snapshots.append(directory.resolve())
    return sorted(set(snapshots), key=str)

def _owned_evidence_snapshot(
    directory: Path, *, now: datetime
) -> tuple[bool, datetime | None]:
    """Validate a dispatcher ownership marker without following links."""
    if directory.is_symlink() or not directory.is_dir():
        return False, None
    owner_path = directory / EVIDENCE_OWNER_FILENAME
    if owner_path.is_symlink() or not owner_path.is_file():
        return False, None
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return False, None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != EVIDENCE_SNAPSHOT_SCHEMA_VERSION
        or not isinstance(payload.get("task_id"), str)
        or not payload["task_id"]
        or created_at.tzinfo is None
        or created_at > now
    ):
        return False, None
    return True, created_at

def sweep_owned_evidence_snapshots(
    *,
    worktree: Path,
    all_owned: bool,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Remove only marker-validated snapshots and report each owned outcome."""
    current = now or datetime.now(UTC)
    root = _audit_root(worktree) / "dispatch-inputs"
    if not root.is_dir() or root.is_symlink():
        return []
    outcomes: list[dict[str, object]] = []
    for task_directory in root.iterdir():
        if task_directory.is_symlink() or not task_directory.is_dir():
            continue
        for directory in task_directory.iterdir():
            owned, created_at = _owned_evidence_snapshot(directory, now=current)
            if not owned or created_at is None:
                continue
            if not all_owned and current - created_at < EVIDENCE_STALE_AFTER:
                continue
            relative = directory.relative_to(worktree.resolve()).as_posix()
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                outcomes.append(
                    {
                        "snapshot": relative,
                        "status": "cleanup-failed",
                        "error": type(exc).__name__,
                    }
                )
            else:
                outcomes.append({"snapshot": relative, "status": "removed"})
        try:
            task_directory.rmdir()
        except OSError:
            pass
    return outcomes

def validate_evidence_inputs(
    *,
    worktree: Path,
    primary_repo: Path,
    evidence_paths: Sequence[str],
    _allow_absent: bool = False,
    _allow_directories: bool = False,
) -> list[tuple[Path, str, str]]:
    """Resolve and validate declared evidence without mutating the worktree."""
    if not evidence_paths:
        raise DispatchError("evidence-unavailable: no evidence paths declared")
    if len(evidence_paths) > EVIDENCE_MAX_FILES:
        raise DispatchError("evidence-unavailable: evidence file count exceeds limit")
    resolved_worktree = worktree.resolve()
    primary_audit = _audit_root(primary_repo).resolve()
    resolved_inputs: list[tuple[Path, str, str]] = []
    seen: set[Path] = set()
    total_bytes = 0
    for raw_path in evidence_paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = resolved_worktree / candidate
        unresolved = candidate.absolute()
        if unresolved.is_symlink():
            raise DispatchError("evidence-unavailable: symlinks are unsupported")
        resolved = candidate.resolve()
        if resolved in seen:
            raise DispatchError("evidence-unavailable: duplicate evidence input")
        seen.add(resolved)
        if not resolved.exists() and not _allow_absent:
            raise DispatchError("evidence-unavailable: evidence file is missing")
        if (
            resolved.exists()
            and not resolved.is_file()
            and not (_allow_directories and resolved.is_dir())
        ):
            raise DispatchError("evidence-unavailable: evidence input is not a file")
        if resolved.exists() and not os.access(resolved, os.R_OK):
            raise DispatchError("evidence-unavailable: evidence file is unreadable")
        if resolved.is_relative_to(resolved_worktree):
            root_kind = "worktree"
            relative = resolved.relative_to(resolved_worktree).as_posix()
        elif resolved.is_relative_to(primary_audit):
            root_kind = "primary-audit"
            relative = resolved.relative_to(primary_audit).as_posix()
        else:
            raise DispatchError(
                "evidence-unavailable: evidence path is outside allowed roots"
            )
        if not resolved.exists() and root_kind != "worktree":
            raise DispatchError(
                "evidence-unavailable: absent evidence must be a worktree path"
            )
        size = resolved.stat().st_size if resolved.is_file() else 0
        if size > EVIDENCE_MAX_FILE_BYTES:
            raise DispatchError(
                "evidence-unavailable: evidence file exceeds size limit"
            )
        total_bytes += size
        if total_bytes > EVIDENCE_MAX_TOTAL_BYTES:
            raise DispatchError(
                "evidence-unavailable: evidence total exceeds size limit"
            )
        resolved_inputs.append((resolved, root_kind, relative))
    return resolved_inputs

def _delivery_merge_delta_manifest(path: Path) -> dict[str, object] | None:
    """Validate the optional compact path coverage manifest carried as evidence."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        MERGE_DELTA_PATH_MANIFEST_SCHEMA_VERSION
    ):
        return None
    digest = payload.get("merge_delta_digest")
    paths = payload.get("paths")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DispatchError(
            "evidence-unavailable: merge-delta manifest digest is invalid"
        )
    if not isinstance(paths, list) or not paths:
        raise DispatchError(
            "evidence-unavailable: merge-delta manifest paths are invalid"
        )
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in paths:
        if not isinstance(entry, dict) or set(entry) != {"path", "blob_sha"}:
            raise DispatchError(
                "evidence-unavailable: merge-delta manifest path entry is invalid"
            )
        candidate = entry.get("path")
        blob_sha = entry.get("blob_sha")
        if (
            not isinstance(candidate, str)
            or not candidate
            or candidate.startswith("/")
            or ".." in Path(candidate).parts
            or candidate in seen
            or (
                blob_sha is not None
                and (
                    not isinstance(blob_sha, str)
                    or re.fullmatch(r"[0-9a-f]{40}", blob_sha) is None
                )
            )
        ):
            raise DispatchError(
                "evidence-unavailable: merge-delta manifest path entry is invalid"
            )
        seen.add(candidate)
        normalized.append({"path": candidate, "blob_sha": blob_sha})
    return {
        "schema_version": MERGE_DELTA_PATH_MANIFEST_SCHEMA_VERSION,
        "merge_delta_digest": digest,
        "paths": normalized,
    }

def stage_evidence_snapshot(
    *,
    worktree: Path,
    primary_repo: Path,
    task_id: str,
    evidence_paths: Sequence[str],
    _allow_empty: bool = False,
    _allow_absent: bool = False,
    _allow_directories: bool = False,
) -> EvidenceSnapshot:
    """Copy declared repository evidence into one immutable worker snapshot."""
    resolved_inputs = (
        []
        if _allow_empty and not evidence_paths
        else validate_evidence_inputs(
            worktree=worktree,
            primary_repo=primary_repo,
            evidence_paths=evidence_paths,
            _allow_absent=_allow_absent,
            _allow_directories=_allow_directories,
        )
    )

    resolved_worktree = worktree.resolve()
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    parent = _audit_root(resolved_worktree) / "dispatch-inputs" / safe_task_id
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = parent / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    manifest: list[dict[str, object]] = []
    files: list[Path] = []
    try:
        owner_path = directory / EVIDENCE_OWNER_FILENAME
        owner_path.write_text(
            json.dumps(
                {
                    "schema_version": EVIDENCE_SNAPSHOT_SCHEMA_VERSION,
                    "task_id": task_id,
                    "created_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        owner_path.chmod(0o400)
        for index, (source, root_kind, relative) in enumerate(resolved_inputs):
            destination = directory / f"{index:02d}-{source.name}"
            if not source.exists():
                manifest.append(
                    {
                        "origin": f"{root_kind}/{relative}",
                        "path": destination.relative_to(
                            resolved_worktree
                        ).as_posix(),
                        "state": "absent",
                    }
                )
                files.append(destination)
                continue
            if source.is_dir():
                destination.mkdir(mode=0o500)
                manifest.append(
                    {
                        "origin": f"{root_kind}/{relative}",
                        "path": destination.relative_to(
                            resolved_worktree
                        ).as_posix(),
                        "state": "directory",
                    }
                )
                files.append(destination)
                continue
            # shutil.copy2, not Path.copy: the latter is Python 3.14-only and
            # this script must run under system Python (#3000).
            shutil.copy2(source, destination)
            destination.chmod(0o400)
            entry = {
                "origin": f"{root_kind}/{relative}",
                "path": destination.relative_to(resolved_worktree).as_posix(),
                "sha256": _evidence_sha256(destination),
                "size": destination.stat().st_size,
            }
            merge_delta_manifest = _delivery_merge_delta_manifest(destination)
            if merge_delta_manifest is not None:
                entry["delivery_merge_delta_manifest"] = merge_delta_manifest
            manifest.append(entry)
            files.append(destination)
        manifest_path = directory / EVIDENCE_MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        manifest_path.chmod(0o400)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return EvidenceSnapshot(
        directory=directory,
        files=tuple(files),
        manifest=tuple(manifest),
        task_id=task_id,
    )


def trust_claim_evidence_scope(task: object) -> tuple[str, ...]:
    """Return the only repository paths a trust-claim delta may expose."""
    from .trust_claims import TrustClaimTaskV1, _canonical_path

    if isinstance(task, TrustClaimTaskV1):
        claims: object = task.invalidated_claims
        retirements: object = task.retirements
        changed: object = task.changed_paths
    elif isinstance(task, Mapping):
        claims = task.get("invalidated_claims")
        retirements = task.get("retirements", [])
        changed = task.get("changed_paths")
    else:
        raise DispatchError("trust-claim evidence task is invalid")
    if (
        not isinstance(claims, (list, tuple))
        or not isinstance(retirements, (list, tuple))
        or not isinstance(changed, (list, tuple))
    ):
        raise DispatchError("trust-claim evidence task is invalid")
    allowed: set[str] = set()
    try:
        allowed.update(_canonical_path(path, label="changed_paths") for path in changed)
        for claim in (*claims, *retirements):
            raw_paths = (
                claim.paths
                if hasattr(claim, "paths")
                else (claim.get("paths") if isinstance(claim, Mapping) else None)
            )
            if not isinstance(raw_paths, (list, tuple)):
                raise DispatchError("trust-claim evidence claim is invalid")
            allowed.update(
                _canonical_path(path, label="trust claim paths")
                for path in raw_paths
            )
    except ValueError as exc:
        raise DispatchError(f"trust-claim evidence scope is invalid: {exc}") from exc
    return tuple(sorted(allowed))


def _expanded_trust_claim_evidence_paths(
    worktree: Path, allowed: Sequence[str]
) -> tuple[str, ...]:
    """Expand declared directories to their current, bounded file inventory."""
    resolved_worktree = worktree.resolve()
    selected: set[str] = set()
    for path in allowed:
        candidate = resolved_worktree / path
        if not candidate.is_dir():
            selected.add(path)
            continue
        descendants = [
            child.relative_to(resolved_worktree).as_posix()
            for child in candidate.rglob("*")
            if child.is_file() or child.is_symlink()
        ]
        if descendants:
            selected.update(descendants)
        else:
            selected.add(path)
    if len(selected) > EVIDENCE_MAX_FILES:
        raise EvidenceScopeTooLargeError(len(selected))
    return tuple(sorted(selected))


def _trust_claim_evidence_is_scoped(path: str, allowed: Sequence[str]) -> bool:
    from .trust_claims import _path_covers

    return any(_path_covers(root, path) for root in allowed)


def _trust_claim_paths(task: object, field: str) -> tuple[str, ...]:
    from .trust_claims import _canonical_path

    raw_claims = (
        getattr(task, field)
        if hasattr(task, field)
        else task.get(field, [])
        if isinstance(task, Mapping)
        else None
    )
    if not isinstance(raw_claims, (list, tuple)):
        raise DispatchError("trust-claim evidence task is invalid")
    paths: set[str] = set()
    try:
        for claim in raw_claims:
            raw_paths = (
                claim.paths
                if hasattr(claim, "paths")
                else claim.get("paths")
                if isinstance(claim, Mapping)
                else None
            )
            if not isinstance(raw_paths, (list, tuple)):
                raise DispatchError("trust-claim evidence claim is invalid")
            paths.update(
                _canonical_path(path, label="trust claim paths") for path in raw_paths
            )
    except ValueError as exc:
        raise DispatchError(f"trust-claim evidence scope is invalid: {exc}") from exc
    return tuple(sorted(paths))


def _previous_tree_retirement_entries(
    primary_repo: Path,
    tree_sha: object,
    roots: Sequence[str],
    *,
    current_count: int,
) -> tuple[tuple[str, bytes | None], ...]:
    """Read retired coverage from the bound previous tree, never the worktree."""
    if not roots:
        return ()
    if (
        not isinstance(tree_sha, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", tree_sha) is None
    ):
        raise DispatchError("trust-claim retirement evidence tree is invalid")
    environment = sandbox_environment(os.environ)
    listed = subprocess.run(
        trusted_git_command(
            primary_repo.resolve(),
            "--literal-pathspecs",
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            tree_sha,
            "--",
            *roots,
        ),
        capture_output=True,
        check=False,
        env=environment,
    )
    if listed.returncode != 0:
        raise DispatchError("evidence-unavailable: previous trust tree is unavailable")
    try:
        paths = tuple(
            path.decode("utf-8") for path in listed.stdout.split(b"\0") if path
        )
    except UnicodeDecodeError as exc:
        raise DispatchError(
            "evidence-unavailable: previous trust tree path is invalid"
        ) from exc
    selected: set[str] = set()
    absent: set[str] = set()
    for root in roots:
        matches = [
            path for path in paths if _trust_claim_evidence_is_scoped(path, (root,))
        ]
        if not matches:
            absent.add(root)
        selected.update(matches)
    total_count = current_count + len(selected) + len(absent)
    if total_count > EVIDENCE_MAX_FILES:
        raise EvidenceScopeTooLargeError(total_count)
    entries: list[tuple[str, bytes | None]] = [(path, None) for path in sorted(absent)]
    for path in sorted(selected):
        shown = subprocess.run(
            trusted_git_command(
                primary_repo.resolve(), "show", f"{tree_sha}:{path}"
            ),
            capture_output=True,
            check=False,
            env=environment,
        )
        if shown.returncode != 0:
            raise DispatchError(
                "evidence-unavailable: previous trust evidence is unavailable"
            )
        if len(shown.stdout) > EVIDENCE_MAX_FILE_BYTES:
            raise DispatchError(
                "evidence-unavailable: evidence file exceeds size limit"
            )
        entries.append((path, shown.stdout))
    return tuple(entries)


def _append_previous_tree_evidence(
    snapshot: EvidenceSnapshot,
    *,
    worktree: Path,
    tree_sha: str,
    entries: Sequence[tuple[str, bytes | None]],
) -> EvidenceSnapshot:
    if not entries:
        return snapshot
    manifest = list(snapshot.manifest)
    files = list(snapshot.files)
    total_bytes = sum(
        int(entry.get("size", 0))
        for entry in manifest
        if entry.get("state", "present") == "present"
    )
    try:
        for path, content in entries:
            index = len(files)
            destination = snapshot.directory / f"{index:02d}-previous-{Path(path).name}"
            entry: dict[str, object] = {
                "origin": f"previous-tree/{tree_sha}/{path}",
                "path": destination.relative_to(worktree.resolve()).as_posix(),
            }
            if content is None:
                entry["state"] = "absent"
            else:
                total_bytes += len(content)
                if total_bytes > EVIDENCE_MAX_TOTAL_BYTES:
                    raise DispatchError(
                        "evidence-unavailable: evidence total exceeds size limit"
                    )
                destination.write_bytes(content)
                destination.chmod(0o400)
                entry.update(
                    sha256=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                )
            files.append(destination)
            manifest.append(entry)
        manifest_path = snapshot.directory / EVIDENCE_MANIFEST_FILENAME
        manifest_path.chmod(0o600)
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        manifest_path.chmod(0o400)
    except Exception:
        shutil.rmtree(snapshot.directory, ignore_errors=True)
        raise
    return EvidenceSnapshot(
        directory=snapshot.directory,
        files=tuple(files),
        manifest=tuple(manifest),
        task_id=snapshot.task_id,
    )


def stage_trust_claim_evidence(
    *,
    worktree: Path,
    primary_repo: Path,
    task_id: str,
    task: object,
    evidence_paths: Sequence[str] | None = None,
) -> EvidenceSnapshot:
    """Stage a trust delta after enforcing its claim-and-change path boundary."""
    allowed = trust_claim_evidence_scope(task)
    retirement_roots = _trust_claim_paths(task, "retirements")
    changed = (
        task.changed_paths
        if hasattr(task, "changed_paths")
        else task.get("changed_paths")
        if isinstance(task, Mapping)
        else None
    )
    if not isinstance(changed, (list, tuple)):
        raise DispatchError("trust-claim evidence task is invalid")
    current_allowed = tuple(
        sorted(set(changed) | set(_trust_claim_paths(task, "invalidated_claims")))
    )
    if evidence_paths is None:
        if isinstance(task, Mapping) and "evidence_inventory" in task:
            declared = task.get("evidence_inventory")
            if not isinstance(declared, (list, tuple)):
                raise DispatchError("trust-claim evidence inventory is invalid")
            selected = tuple(declared)
        else:
            selected = _expanded_trust_claim_evidence_paths(worktree, current_allowed)
    else:
        selected = tuple(evidence_paths)
    declared_inventory = (
        task.get("evidence_inventory") if isinstance(task, Mapping) else None
    )
    if declared_inventory is not None and (
        not isinstance(declared_inventory, (list, tuple))
        or not all(isinstance(path, str) for path in declared_inventory)
        or not all(
            _trust_claim_evidence_is_scoped(path, allowed)
            for path in declared_inventory
        )
    ):
        raise DispatchError("trust-claim evidence inventory exceeds task scope")
    from .trust_claims import _canonical_path

    if not all(isinstance(path, str) for path in selected) or len(selected) != len(
        set(selected)
    ):
        raise DispatchError("trust-claim evidence inventory exceeds task scope")
    try:
        normalized_selected = tuple(
            _canonical_path(path, label="trust claim evidence path")
            for path in selected
        )
    except ValueError as exc:
        raise DispatchError(
            f"trust-claim evidence inventory is invalid: {exc}"
        ) from exc
    if not all(
        _trust_claim_evidence_is_scoped(path, allowed)
        for path in normalized_selected
    ):
        raise DispatchError("trust-claim evidence inventory exceeds task scope")
    tree_sha = (
        task.delta_from_tree_sha
        if hasattr(task, "delta_from_tree_sha")
        else task.get("delta_from_tree_sha")
        if isinstance(task, Mapping)
        else None
    )
    previous_entries = _previous_tree_retirement_entries(
        primary_repo, tree_sha, retirement_roots, current_count=len(normalized_selected)
    )
    total_count = len(normalized_selected) + len(previous_entries)
    if total_count > EVIDENCE_MAX_FILES:
        raise EvidenceScopeTooLargeError(total_count)
    snapshot = stage_evidence_snapshot(
        worktree=worktree,
        primary_repo=primary_repo,
        task_id=task_id,
        evidence_paths=normalized_selected,
        _allow_empty=True,
        _allow_absent=True,
        _allow_directories=True,
    )
    return _append_previous_tree_evidence(
        snapshot,
        worktree=worktree,
        tree_sha=tree_sha,  # type: ignore[arg-type]
        entries=previous_entries,
    )


stage_trust_claim_evidence_snapshot = stage_trust_claim_evidence

def verify_evidence_snapshot(snapshot: EvidenceSnapshot) -> None:
    """Fail when copied evidence changes between engine attempts or acceptance."""
    if len(snapshot.files) != len(snapshot.manifest):
        raise DispatchError("evidence-drift: snapshot manifest shape changed")
    for path, entry in zip(snapshot.files, snapshot.manifest, strict=True):
        state = entry.get("state", "present")
        if state == "absent":
            if path.exists():
                raise DispatchError("evidence-drift: absent evidence appeared")
            continue
        if state == "directory":
            if not path.is_dir():
                raise DispatchError("evidence-drift: snapshot directory is missing")
            continue
        if state != "present":
            raise DispatchError("evidence-drift: snapshot state is invalid")
        if not path.is_file():
            raise DispatchError("evidence-drift: snapshot file is missing")
        if path.stat().st_size != entry.get("size"):
            raise DispatchError("evidence-drift: snapshot size changed")
        if _evidence_sha256(path) != entry.get("sha256"):
            raise DispatchError("evidence-drift: snapshot content changed")

def mirror_evidence_into_review_snapshot(
    *,
    worktree: Path,
    evidence_snapshot: EvidenceSnapshot,
    review_snapshot: ReviewSnapshot,
) -> EvidenceSnapshot:
    """Expose immutable declared inputs inside an immutable review checkout.

    Evidence paths in the manifest are relative to the governed worktree. Copy
    the dispatcher-owned snapshot to the same relative location below the
    detached review checkout so the worker can read it without gaining access
    to the mutable delivery tree or its external Git metadata.
    """
    relative = evidence_snapshot.directory.relative_to(worktree.resolve())
    destination = review_snapshot.directory / relative
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(
        evidence_snapshot.directory, destination, copy_function=shutil.copy2
    )
    mirrored_files = tuple(
        review_snapshot.directory / str(entry["path"])
        for entry in evidence_snapshot.manifest
    )
    return EvidenceSnapshot(
        directory=destination,
        files=mirrored_files,
        manifest=evidence_snapshot.manifest,
        task_id=evidence_snapshot.task_id,
    )
