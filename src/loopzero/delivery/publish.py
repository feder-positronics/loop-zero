"""Publish a ready PR with final content and one batched label mutation."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from ..review import _tree_coverage as review_tree_coverage
from ..review import risk as delivery_review_risk
from ._visual_evidence import evaluate as evaluate_visual_evidence
from ..kernel.authority_projection import (
    authenticated_coordinator_record_ids,
    authenticated_supersessions,
)
from ..kernel.authority_store import (
    active_outer_run_id,
)
from ..kernel.gitscope import (
    DispatchError,
)
from ..review.authority import (
    accepted_review_terminals,
    authenticated_review_terminals,
    authenticated_verdicts,
    latest_accepted_review_terminal,
    review_terminal_acceptance_reasons,
)
from ..review.findings import (
    LedgerReadError,
    closure_authority_severity,
    load_finding_records,
)
from ..kernel.patch_identity import (
    equivalence_receipt_is_valid,
    prove_patch_equivalence,
    validate_patch_carry,
)
from ._body_check import has_standalone_reason, validate_contract
from ..integrations.github import GateError, SecureGitRunner
from ._publish_body import (
    EVIDENCE_END,
    EVIDENCE_START,
    bind_loopzero_evidence,
    validate_loopzero_evidence as _source_validate_loopzero_evidence,
    validate_body_against_trusted_base,
    validate_adopted_body,
)
from ._publish_obligations import (
    require_obligation_acknowledgment as _source_require_obligation_acknowledgment,
)
from ._publish_findings import (
    _emit_orphan_finding_warning,
    resolved_loopzero_predecessors,
)
from ._publish_gate import format_orphan_finding_warning, inspect_exact_finding_ids
from ._publish_paths import (
    PublicationError,
    changed_paths_between,
)
from ._publish_threads import require_resolved_review_threads as _source_require_resolved_review_threads
from ._publish_paths import (
    require_local_publication_prerequisites as _require_local_prerequisites,
)
from ._publish_remote import (
    converge_remote_publication_head,
    trusted_publication_base_head,
)
from ._publish_risk import (
    _security_trigger_paths_between,
    bind_review_reentry,
    load_review_risk_file,
    review_risk_envelope_for_publication,
)
from ._publish_risk import (
    all_dispatch_records as _all_dispatch_records,
)
from ._publish_risk import (
    write_evidence as _write_evidence,
)
from ._publish_risk import (
    write_published_evidence_once as _write_published_evidence_once,
)
from ..kernel.run_identity import extract_run_id_marker
from ..kernel.worktree_lease import (
    WorktreeGuardError,
    identities_match,
    worktree_lease,
)


class JsonRunner(Protocol):
    def run_json(
        self,
        args: list[str],
        *,
        payload: dict[str, object] | None = None,
    ) -> object: ...


class CommandOutcome(Protocol):
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> CommandOutcome: ...


class SubprocessRunner:
    """JSON adapter over the same repository-bound runner used for Git."""

    def __init__(self, command_runner: CommandRunner) -> None:
        self.command_runner = command_runner

    def run_json(
        self,
        args: list[str],
        *,
        payload: dict[str, object] | None = None,
    ) -> object:
        completed = self.command_runner.run(
            args,
            check=False,
            input_text=json.dumps(payload) if payload is not None else None,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise PublicationError(
                f"`{' '.join(args)}` failed with exit {completed.returncode}: {detail}"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PublicationError(
                f"`{' '.join(args)}` did not return valid JSON"
            ) from exc


@dataclass(frozen=True)
class PublicationRequest:
    title: str
    body: str
    head: str
    base: str
    labels: tuple[str, ...] = ()
    expected_head: str = ""
    review_task_id: str = ""
    review_source_identity: Mapping[str, object] | None = None
    current_source_identity: Mapping[str, object] | None = None
    review_snapshot_tree_sha: str | None = None
    current_tree_sha: str | None = None
    chain_covers_head: bool = False
    review_patch_identity: Mapping[str, object] | None = None
    current_patch_identity: Mapping[str, object] | None = None
    patch_equivalence: Mapping[str, object] | None = None
    open_finding_ids: tuple[str, ...] = ()
    current_clean: bool = True
    lifecycle_violations: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    known_finding_ids: tuple[str, ...] = ()
    finding_provenance_errors: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    review_risk_json: str = ""
    review_risk_sha256: str = ""
    review_risk_tier: str = ""
    admission_tier_floor: str = ""
    review_reentry_generation: int = 0


@dataclass(frozen=True)
class PublicationResult:
    number: int
    url: str
    evidence_path: Path
    post_open_metadata_mutations: int


@dataclass(frozen=True, slots=True)
class BodyContract:
    """Configured body headings for package consumers."""

    required_sections: tuple[str, ...]
    standalone_label_key: str = "standalone"


_SOURCE_VALIDATE_CONTRACT = validate_contract
write_evidence = _write_evidence
write_published_evidence_once = _write_published_evidence_once


def validate_loopzero_evidence(body: str, **kwargs) -> None:
    try:
        _source_validate_loopzero_evidence(body, **kwargs)
    except ValueError as exc:
        raise PublicationError(str(exc)) from exc


def validate_contract(
    body: str,
    *,
    contract: BodyContract | None = None,
    draft: bool = False,
    standalone: bool = False,
) -> str | None:
    """Validate the moved contract or a consumer-configured heading contract."""
    if contract is None:
        return _SOURCE_VALIDATE_CONTRACT(
            body, draft=draft, standalone=standalone
        )
    if body.count("<!--") != body.count("-->"):
        return "PR body contains an unclosed HTML comment."
    for section in contract.required_sections:
        match = re.search(
            rf"(?ims)^##\s+{re.escape(section)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
            body,
        )
        if match is None:
            return f"Missing required '## {section}' section."
        visible = re.sub(r"<!--.*?-->|```.*?```", "", match.group("body"), flags=re.DOTALL)
        if not any(character.isalnum() for character in visible):
            return f"Required '## {section}' section must contain a result."
    if draft or standalone or has_standalone_reason(body) or re.search(
        r"(?im)^\s*(?:closes|fixes|refs|reverts)\s+(?:[^\n]*\s)?#\d+\b", body
    ):
        return None
    return "No work reference found and no standalone reason was supplied."


def _repo_root(runner: CommandRunner | None = None) -> Path:
    if runner is not None:
        authority_repo = getattr(runner, "authority_repo", None)
        if isinstance(authority_repo, Path):
            return authority_repo
        common_git_dir = runner.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
        return Path(common_git_dir).parent
    common_git_dir = subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    return Path(common_git_dir).parent


def require_canonical_publisher_source(
    authority_repo: Path, *, source_path: Path | None = None
) -> None:
    """Reject a publisher loaded from a delivery branch or stale worktree."""
    expected = (
        authority_repo.resolve() / "scripts" / "util" / "pr_publish.py"
    ).resolve()
    actual = (source_path or Path(__file__)).resolve()
    if actual != expected:
        raise PublicationError(
            "PR publication must run the canonical primary publisher. Retry with "
            f"`/usr/bin/python3 -I {expected} ...` from the delivery worktree."
        )


def _head_from_pr(payload: Mapping[str, object]) -> tuple[str | None, str | None]:
    raw_head = payload.get("head")
    if not isinstance(raw_head, dict):
        return None, None
    ref = raw_head.get("ref")
    sha = raw_head.get("sha")
    return (
        ref if isinstance(ref, str) else None,
        sha if isinstance(sha, str) else None,
    )


def _base_from_pr(payload: Mapping[str, object]) -> str | None:
    raw_base = payload.get("base")
    if not isinstance(raw_base, dict):
        return None
    ref = raw_base.get("ref")
    return ref if isinstance(ref, str) else None


def _head_repository_from_pr(payload: Mapping[str, object]) -> str | None:
    raw_head = payload.get("head")
    if not isinstance(raw_head, dict):
        return None
    raw_repo = raw_head.get("repo")
    if not isinstance(raw_repo, dict):
        return None
    full_name = raw_repo.get("full_name")
    return full_name if isinstance(full_name, str) else None


def _quarantine(
    runner: JsonRunner,
    *,
    number: int,
) -> None:
    try:
        runner.run_json(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                "--input",
                "-",
                f"repos/{{owner}}/{{repo}}/pulls/{number}",
            ],
            payload={"state": "closed"},
        )
    except PublicationError as exc:
        raise PublicationError(
            f"PR #{number} head mismatched and quarantine cleanup failed"
        ) from exc


def _review_record_lens(record: Mapping[str, object]) -> str:
    return review_tree_coverage.review_record_lens(record)


def _has_delivery_lens(record: Mapping[str, object], lens: str) -> bool:
    return review_tree_coverage.terminal_has_review_section(record, lens)


def load_review_source_identity(
    repo: Path,
    task_id: str,
    *,
    expected_lens: str = "code",
    dispatch_records: Sequence[dict[str, object]] | None = None,
) -> tuple[dict[str, object], str | None, str]:
    """Load one accepted, non-superseded final-review source receipt.

    Returns the V2 source identity, the review's snapshot tree SHA when the
    review was snapshot-bound (#3106 A2 L1) else None, and the review lens
    used to scope publication delta edges to the same lens.
    """
    records = (
        dispatch_records
        if dispatch_records is not None
        else _all_dispatch_records(repo)
    )
    task_records = [record for record in records if record.get("task_id") == task_id]
    superseded_task_ids = {
        str(record.get("superseded_task_id") or record.get("task_id"))
        for record in authenticated_supersessions(records)
        if record.get("superseded_task_id") or record.get("task_id")
    }
    if task_id in superseded_task_ids:
        raise PublicationError(f"final review task {task_id!r} was superseded")
    projected_terminals = accepted_review_terminals(records)
    terminal = projected_terminals.get(task_id)
    if terminal is None:
        rejected = [
            record
            for record in task_records
            if record.get("type") in {"attempt-terminal", "attempt-recovery"}
        ]
        details = (
            ": " + "; ".join(review_terminal_acceptance_reasons(rejected[-1]))
            if rejected
            else ""
        )
        raise PublicationError(
            f"final review task {task_id!r} has no accepted read-only attempt" + details
        )
    latest_verdict_record = authenticated_verdicts(
        records, _accepted_terminals=projected_terminals
    ).get(task_id)
    latest_verdict = (
        latest_verdict_record.get("verdict")
        if latest_verdict_record is not None
        else None
    )
    if latest_verdict != "pass":
        raise PublicationError(
            f"final review task {task_id!r} latest independent verdict is not pass"
        )
    if not _has_delivery_lens(terminal, expected_lens):
        raise PublicationError(
            f"final review task {task_id!r} is not a hash-bound "
            f"delivery-code-review/{expected_lens} review"
        )
    identity = terminal.get("source_identity")
    if not isinstance(identity, dict) or identity.get("version") != 2:
        raise PublicationError(
            f"final review task {task_id!r} has no V2 source identity"
        )
    snapshot_tree = terminal.get("snapshot_tree_sha")
    evidence_lens = (
        expected_lens
        if isinstance(terminal.get("review_chain_receipt"), dict)
        else _review_record_lens(terminal)
    )
    return (
        identity,
        snapshot_tree if isinstance(snapshot_tree, str) else None,
        evidence_lens,
    )


def accepted_review_patch_identity(
    records: Sequence[dict[str, object]], task_id: str
) -> Mapping[str, object] | None:
    """Return the persisted identity from one already-admitted review terminal."""
    terminal = latest_accepted_review_terminal(records, task_id)
    return review_tree_coverage.accepted_review_patch_identity(terminal)


def carried_review_patch_identity(
    repo: Path,
    records: Sequence[dict[str, object]],
    task_id: str,
    expected_head: str,
    *,
    current_source: Mapping[str, object] | None = None,
) -> Mapping[str, object] | None:
    """Return one recomputed captured identity for the exact publication head."""
    terminal = latest_accepted_review_terminal(records, task_id)
    return review_tree_coverage.carried_review_patch_identity(
        repo,
        records,
        terminal,
        task_id,
        expected_head,
        current_source=current_source,
        patch_carry_validator=validate_patch_carry,
    )


def load_delta_edges(
    repo: Path,
    *,
    lens: str,
    source_ref: str,
    runner: CommandRunner | None = None,
    dispatch_records: Sequence[dict[str, object]] | None = None,
    finding_records: Sequence[dict[str, object]] | None = None,
) -> dict[str, str]:
    """Map exact reviewed or proven tree transitions for one standing lens."""
    records = (
        dispatch_records
        if dispatch_records is not None
        else _all_dispatch_records(repo)
    )
    projected_terminals = accepted_review_terminals(records)
    findings_dir = repo / ".audit" / "findings"
    findings = (
        finding_records
        if finding_records is not None
        else (
            load_finding_records(repo, strict_malformed=True)
            if findings_dir.is_dir()
            else ()
        )
    )
    return review_tree_coverage.load_delta_edges(
        repo,
        lens=lens,
        source_ref=source_ref,
        accepted_terminals=projected_terminals,
        latest_verdicts=authenticated_verdicts(
            records,
            _accepted_terminals=projected_terminals,
        ),
        finding_records=findings,
        runner=runner,
        security_classifier=_security_trigger_paths_between,
    )


def chain_covers_tree(
    start_tree: str | None,
    edges: Mapping[str, str],
    target_tree: str | None,
    *,
    max_hops: int = 64,
) -> bool:
    """Walk delta edges from the full review's tree toward the target tree."""
    return review_tree_coverage.chain_covers_tree(
        start_tree,
        edges,
        target_tree,
        max_hops=max_hops,
    )


def open_important_finding_ids(
    repo: Path,
    head_ref: str,
    *,
    dispatch_records: Sequence[dict[str, object]] | None = None,
    finding_records: Sequence[dict[str, object]] | None = None,
    current_review_task_id: str | None = None,
) -> tuple[str, ...]:
    """Open important+ findings from standing formal reviews on this ref.

    The dispatch stream is durable authority and is intentionally excluded
    from audit retention; otherwise old open findings would fail open here.
    """
    records = (
        dispatch_records
        if dispatch_records is not None
        else _all_dispatch_records(repo)
    )
    review_refs: dict[str, str] = {}
    primary_chains: dict[str, dict[str, object]] = {}
    standing_review_terminals = authenticated_review_terminals(records)
    resolved_predecessors = resolved_loopzero_predecessors(
        repo,
        terminals=standing_review_terminals,
        current_review_task_id=current_review_task_id,
        verdicts=authenticated_verdicts(records) if current_review_task_id else {},
    )
    failed_review_supersessions = [
        record
        for record in authenticated_supersessions(records)
        if record.get("supersession_reason") == "failed-review"
    ]
    all_review_terminals = (
        authenticated_review_terminals(records, _superseded_task_ids=frozenset())
        if failed_review_supersessions
        else standing_review_terminals
    )
    for task_id, record in standing_review_terminals.items():
        if record.get("advisory") is True:
            continue
        identity = record.get("source_identity")
        if isinstance(identity, dict):
            review_refs[task_id] = str(identity.get("ref") or "")
        chain_receipt = record.get("review_chain_receipt")
        if isinstance(chain_receipt, dict):
            primary_chains[task_id] = chain_receipt
    # A failed-review supersession removes the producer from standing review
    # evidence before its Finding Ledger batch can be retired. Preserve that
    # exact authenticated producer ref here so a crash in the cross-ledger gap
    # remains publication-blocking until every open finding becomes stale.
    for supersession in failed_review_supersessions:
        task_id = supersession.get("superseded_task_id")
        identity = supersession.get("source_identity")
        if isinstance(task_id, str) and isinstance(identity, dict):
            review_refs[task_id] = str(identity.get("ref") or "")
            terminal = all_review_terminals.get(task_id)
            chain_receipt = (
                terminal.get("review_chain_receipt")
                if isinstance(terminal, dict)
                else None
            )
            if isinstance(chain_receipt, dict):
                primary_chains[task_id] = chain_receipt
    advisory_chain_tasks: set[str] = set()
    authenticated_coordinator_ids = authenticated_coordinator_record_ids(records)
    for record in records:
        if (
            record.get("type") != "review-chain-advisory"
            or record.get("status") != "completed"
            or record.get("advisory") is not True
            or id(record) not in authenticated_coordinator_ids
        ):
            continue
        task_id = record.get("task_id")
        primary_task_id = record.get("primary_task_id")
        receipt = record.get("review_chain_advisory_receipt")
        primary = primary_chains.get(str(primary_task_id))
        if (
            not isinstance(task_id, str)
            or not isinstance(receipt, dict)
            or primary is None
            or receipt.get("schema_version") != "ReviewChainAdvisoryReceiptV1"
            or receipt.get("advisory_task_id") != task_id
            or receipt.get("primary_task_id") != primary_task_id
            or receipt.get("chain_id") != primary.get("chain_id")
            or receipt.get("snapshot_tree_sha") != primary.get("snapshot_tree_sha")
        ):
            continue
        review_refs[task_id] = review_refs.get(str(primary_task_id), "")
        advisory_chain_tasks.add(task_id)
    blockers = [
        str(record.get("finding_id"))
        for record in (
            finding_records
            if finding_records is not None
            else load_finding_records(repo, strict_malformed=True)
        )
        if record.get("state") == "open"
        and record.get("review_task_id") not in resolved_predecessors
        and closure_authority_severity(record) in {"critical", "important"}
        and (
            record.get("advisory") is not True
            or record.get("review_task_id") in advisory_chain_tasks
        )
        and review_refs.get(str(record.get("review_task_id"))) == head_ref
    ]
    return tuple(sorted(blockers))


def require_active_publication_run(
    *,
    run_id: str,
    worktree: Path,
    authority_repo: Path | None = None,
    branch: str | None = None,
) -> None:
    """Bind the PR marker to the sole active outer run on this branch."""
    active_run = (
        active_outer_run_id(
            worktree,
            authority_repo=authority_repo,
            branch=branch,
        )
        if authority_repo is not None
        else active_outer_run_id(worktree)
    )
    if active_run != run_id:
        raise PublicationError(
            "PR skill-run marker does not identify the active outer run"
        )


def require_local_publication_prerequisites(
    *,
    worktree: Path | None = None,
    head: str,
    expected_head: str,
    runner: CommandRunner | None = None,
) -> None:
    """Fail before authority reads unless this checkout is publishable as named."""
    _require_local_prerequisites(
        worktree=worktree or Path.cwd(),
        head=head,
        expected_head=expected_head,
        runner=runner or SecureGitRunner(worktree or Path.cwd()),
    )


def require_obligation_acknowledgment(*args, **kwargs) -> None:
    """Preserve source policy while permitting a configured evaluator seam."""
    evaluator = kwargs.pop("evaluator", None)
    if evaluator is None:
        _source_require_obligation_acknowledgment(*args, **kwargs)
        return
    base = kwargs.pop("base")
    head = kwargs.pop("head")
    body = kwargs.pop("body")
    if args or kwargs:
        raise TypeError("unexpected obligation acknowledgment arguments")
    try:
        missing = evaluator(base, head, acknowledgment_text=body)
    except Exception as exc:
        raise PublicationError(f"cannot evaluate obligation changes: {exc}") from exc
    if missing:
        raise PublicationError(
            "changed obligations require a substantive acknowledgment"
        )


def require_resolved_review_threads(runner, pr_url: str | None = None, *, pr: int | None = None) -> None:
    """Read all review-thread pages through either supported GitHub seam."""
    if pr is None:
        _source_require_resolved_review_threads(runner, str(pr_url or ""))
        return
    repository = runner.repository()
    cursor: str | None = None
    query = (
        "query($owner:String!,$name:String!,$pr:Int!,$after:String){repository(owner:$owner,name:$name){"
        "pullRequest(number:$pr){reviewThreads(first:100,after:$after){nodes{id isResolved path comments(first:1){nodes{url}}}"
        "pageInfo{hasNextPage endCursor}}}}}"
    )
    unresolved: list[object] = []
    while True:
        payload = runner.api(
            "graphql",
            fields={"query": query, "variables": {
                "owner": repository.owner, "name": repository.name,
                "pr": pr, "after": cursor,
            }},
        )
        try:
            page = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes, info = page["nodes"], page["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise PublicationError("GitHub returned malformed review-thread evidence") from exc
        if not isinstance(nodes, list) or not isinstance(info, Mapping):
            raise PublicationError("GitHub returned malformed review-thread evidence")
        unresolved.extend(node for node in nodes if isinstance(node, dict) and node.get("isResolved") is False)
        if info.get("hasNextPage") is not True:
            break
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise PublicationError("GitHub review-thread pagination is malformed")
    if unresolved:
        raise PublicationError(f"PR has {len(unresolved)} unresolved review thread(s), including outdated threads")


def write_publication_intent(
    evidence_dir: Path, request: PublicationRequest
) -> Path:
    """Durably record an idempotency key before the PR-creation effect."""
    record = {
        "type": "publication-intent",
        "base": request.base,
        "body_sha256": hashlib.sha256(request.body.encode()).hexdigest(),
        "expected_head": request.expected_head,
        "head": request.head,
        "review_task_id": request.review_task_id or None,
        "title": request.title,
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    directory = evidence_dir / "intents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != encoded + b"\n":
            raise PublicationError("publication intent conflicts with durable authority")
        return path
    descriptor, temporary = tempfile.mkstemp(prefix=".intent-", dir=directory)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def secure_source_identity(runner: CommandRunner) -> dict[str, object]:
    """Reproduce the clean V2 identity through the sanitized Git boundary."""
    head = runner.run(["git", "rev-parse", "HEAD"]).stdout.strip()
    symbolic = runner.run(["git", "symbolic-ref", "--quiet", "HEAD"], check=False)
    ref = symbolic.stdout.strip() if symbolic.returncode == 0 else "<detached>"
    index = runner.run(["git", "ls-files", "--stage", "-z"]).stdout.encode()
    status = runner.run(
        ["git", "status", "--porcelain=v2", "-z", "-uall"]
    ).stdout.encode()
    state = {
        "head": head,
        "index_sha256": hashlib.sha256(index).hexdigest(),
        "paths": {},
        "status_sha256": hashlib.sha256(status).hexdigest(),
    }
    return {
        "version": 2,
        "ref": ref,
        "head": head,
        "state_sha256": hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def require_remote_publication_prerequisites(
    runner: JsonRunner, *, head: str, expected_head: str
) -> Mapping[str, object]:
    """Prove the named remote branch before scanning local authority ledgers."""
    encoded_head = quote(head, safe="")
    remote = runner.run_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{{owner}}/{{repo}}/branches/{encoded_head}",
        ]
    )
    if not isinstance(remote, dict):
        raise PublicationError("GitHub returned an unexpected branch payload")
    raw_commit = remote.get("commit")
    remote_sha = raw_commit.get("sha") if isinstance(raw_commit, dict) else None
    if remote_sha != expected_head:
        raise PublicationError("remote branch does not match expected head")
    return remote


def verify_request_patch_equivalence(request: PublicationRequest, repo: Path) -> bool:
    """Recompute a request's replay proof from immutable Git objects."""
    if (
        not isinstance(request.review_patch_identity, Mapping)
        or not isinstance(request.current_patch_identity, Mapping)
        or not equivalence_receipt_is_valid(request.patch_equivalence)
    ):
        return False
    recomputed = prove_patch_equivalence(
        repo, request.review_patch_identity, request.current_patch_identity
    )
    return recomputed is not None and recomputed == dict(request.patch_equivalence)


def validate_publication_request(
    request: PublicationRequest, *, patch_identity_covers_head: bool = False
) -> None:
    """Apply all mutation-free publication admission checks."""
    labels = tuple(sorted(set(request.labels)))
    if "standalone" in labels and not has_standalone_reason(request.body):
        raise PublicationError(
            "A ready standalone PR requires a non-empty Standalone-Reason in "
            "its initial body so the opened-event policy is deterministic."
        )
    if error := validate_contract(request.body):
        raise PublicationError(error)
    visual_evidence = evaluate_visual_evidence(
        list(request.changed_paths), request.body
    )
    if not visual_evidence.ok:
        # Advisory since PIL-VISUAL-DEMOTE-1 (2026-08-28): blocking produced a
        # 75% bypass-footer rate instead of specs. The signal stays visible;
        # publication proceeds.
        print(
            "warning: visual-evidence advisory: " + visual_evidence.reason,
            file=sys.stderr,
        )
    if request.lifecycle_violations:
        raise PublicationError(
            "dispatch lifecycle incomplete: " + "; ".join(request.lifecycle_violations)
        )
    if request.finding_provenance_errors:
        raise PublicationError(
            "malformed Finding Ledger trailer: "
            + "; ".join(request.finding_provenance_errors)
        )
    if request.open_finding_ids:
        raise PublicationError(
            "open important+ findings block publication: "
            + ", ".join(request.open_finding_ids[:10])
        )
    if re.fullmatch(r"[0-9a-f]{40}", request.expected_head) is None:
        raise PublicationError("expected head must be exactly 40 lowercase hex")
    try:
        risk_artifact = delivery_review_risk.parse_review_risk(request.review_risk_json)
        if hashlib.sha256(request.review_risk_json.encode("utf-8")).hexdigest() != (
            request.review_risk_sha256
        ):
            raise delivery_review_risk.ReviewRiskError(
                "review-risk artifact digest does not match canonical bytes"
            )
        risk_tier = str(risk_artifact.get("tier") or "")
        if (
            delivery_review_risk.strictest_tier(risk_tier, request.review_risk_tier)
            != request.review_risk_tier
        ):
            raise delivery_review_risk.ReviewRiskError(
                "review-risk tier is weaker than canonical bytes"
            )
        if (
            delivery_review_risk.strictest_tier(risk_tier, request.admission_tier_floor)
            != request.admission_tier_floor
        ):
            raise delivery_review_risk.ReviewRiskError(
                "admission tier floor is weaker than candidate risk"
            )
    except delivery_review_risk.ReviewRiskError as exc:
        raise PublicationError(str(exc)) from exc
    current_source = request.current_source_identity
    if not isinstance(current_source, Mapping):
        raise PublicationError("publication source identities are missing")
    if not request.current_clean:
        raise PublicationError("local source is dirty after final review")
    if current_source.get("ref") != f"refs/heads/{request.head}":
        raise PublicationError("checked-out branch does not match publication head")
    if not request.review_task_id:
        if request.admission_tier_floor != "T0":
            raise PublicationError(
                "publication requires a final review outside deterministic T0"
            )
        if current_source.get("head") != request.expected_head:
            raise PublicationError(
                "local source does not match the expected publication head"
            )
        return
    review_source = request.review_source_identity
    if not isinstance(review_source, Mapping):
        raise PublicationError("publication source identities are missing")
    snapshot_covers_head = (
        request.review_snapshot_tree_sha is not None
        and request.current_tree_sha is not None
        and (
            request.review_snapshot_tree_sha == request.current_tree_sha
            or request.chain_covers_head
            or patch_identity_covers_head
        )
    )
    if current_source.get("head") != request.expected_head or not (
        snapshot_covers_head or identities_match(review_source, current_source)
    ):
        raise PublicationError(
            "local source does not match the accepted final review; republish from "
            "the exact reviewed tree unless a review-chain delta or replay-proven "
            "patch identity covers --expected-head"
        )


def preflight_existing_adoption(
    runner: JsonRunner,
    request: PublicationRequest,
) -> None:
    """Validate a matching open PR before remote branch convergence can mutate it."""
    repository = runner.run_json(
        ["gh", "api", "--method", "GET", "repos/{owner}/{repo}"]
    )
    if not isinstance(repository, dict):
        raise PublicationError("GitHub returned an unexpected repository payload")
    raw_owner = repository.get("owner")
    owner_login = raw_owner.get("login") if isinstance(raw_owner, dict) else None
    repository_name = repository.get("full_name")
    if not isinstance(owner_login, str) or not isinstance(repository_name, str):
        raise PublicationError("GitHub repository payload is missing identity")

    open_prs = runner.run_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "-f",
            "state=open",
            "-f",
            f"head={owner_login}:{request.head}",
            "-f",
            f"base={request.base}",
            "repos/{owner}/{repo}/pulls",
        ]
    )
    if not isinstance(open_prs, list):
        raise PublicationError("GitHub returned an unexpected open-PR payload")
    if not open_prs:
        return
    if len(open_prs) != 1 or not isinstance(open_prs[0], dict):
        raise PublicationError("publication retry found ambiguous open PRs")

    existing = open_prs[0]
    existing_ref, _existing_sha = _head_from_pr(existing)
    number = existing.get("number")
    if (
        existing_ref != request.head
        or _base_from_pr(existing) != request.base
        or _head_repository_from_pr(existing) != repository_name
        or not isinstance(number, int)
    ):
        raise PublicationError("existing PR does not match the adoption request")
    fresh = runner.run_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{{owner}}/{{repo}}/pulls/{number}",
        ]
    )
    if not isinstance(fresh, dict):
        raise PublicationError(
            f"adopted PR #{number} returned an unexpected preflight payload"
        )
    fresh_ref, _fresh_sha = _head_from_pr(fresh)
    if (
        fresh_ref != request.head
        or _base_from_pr(fresh) != request.base
        or _head_repository_from_pr(fresh) != repository_name
    ):
        raise PublicationError(
            f"adopted PR #{number} changed identity during mutation-free preflight"
        )
    validate_adopted_body(
        request, number=number, body=fresh.get("body"), error_type=PublicationError
    )
    require_resolved_review_threads(runner, str(fresh.get("html_url") or ""))


def publish(
    runner: JsonRunner,
    request: PublicationRequest,
    *,
    evidence_dir: Path | None = None,
    remote_branch: Mapping[str, object] | None = None,
    warning_emitted: bool = False,
) -> PublicationResult:
    """Create one ready PR and apply all initial labels in one REST mutation."""
    labels = tuple(sorted(set(request.labels)))
    # Mutation-free admission is also called by ``main`` immediately before
    # remote convergence; repeat it here for direct callers and defense in depth.
    patch_coverage = (
        verify_request_patch_equivalence(request, _repo_root(runner.command_runner))
        if isinstance(runner, SubprocessRunner)
        else False
    )
    validate_publication_request(request, patch_identity_covers_head=patch_coverage)
    if not warning_emitted:
        _emit_orphan_finding_warning(request)

    if remote_branch is None:
        require_remote_publication_prerequisites(
            runner,
            head=request.head,
            expected_head=request.expected_head,
        )

    repository = runner.run_json(
        ["gh", "api", "--method", "GET", "repos/{owner}/{repo}"]
    )
    if not isinstance(repository, dict):
        raise PublicationError("GitHub returned an unexpected repository payload")
    raw_owner = repository.get("owner")
    owner_login = raw_owner.get("login") if isinstance(raw_owner, dict) else None
    repository_name = repository.get("full_name")
    if not isinstance(owner_login, str) or not isinstance(repository_name, str):
        raise PublicationError("GitHub repository payload is missing identity")

    open_prs = runner.run_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "-f",
            "state=open",
            "-f",
            f"head={owner_login}:{request.head}",
            "-f",
            f"base={request.base}",
            "repos/{owner}/{repo}/pulls",
        ]
    )
    if not isinstance(open_prs, list):
        raise PublicationError("GitHub returned an unexpected open-PR payload")

    adopted = False
    if open_prs:
        if len(open_prs) != 1 or not isinstance(open_prs[0], dict):
            raise PublicationError("publication retry found ambiguous open PRs")
        created = open_prs[0]
        existing_ref, existing_sha = _head_from_pr(created)
        if (
            existing_ref != request.head
            or existing_sha != request.expected_head
            or _base_from_pr(created) != request.base
            or _head_repository_from_pr(created) != repository_name
        ):
            number = created.get("number")
            if isinstance(number, int):
                _quarantine(runner, number=number)
                evidence_path = _write_evidence(
                    evidence_dir or _repo_root() / ".audit" / "pr-publications",
                    {
                        "base": request.base,
                        "expected_head": request.expected_head,
                        "head": request.head,
                        "pr": number,
                        "review_task_id": request.review_task_id,
                        "status": "existing_head_mismatch_quarantined",
                    },
                )
                raise PublicationError(
                    "existing PR does not match expected head; "
                    f"evidence: {evidence_path}"
                )
            raise PublicationError("existing PR does not match expected head")
        require_resolved_review_threads(runner, str(created.get("html_url") or ""))
        adopted = True
    else:
        write_publication_intent(
            evidence_dir or _repo_root() / ".audit" / "pr-publications",
            request,
        )
        created = runner.run_json(
            [
                "gh",
                "api",
                "--method",
                "POST",
                "--input",
                "-",
                "repos/{owner}/{repo}/pulls",
            ],
            payload={
                "title": request.title,
                "body": request.body,
                "head": request.head,
                "base": request.base,
                "draft": False,
            },
        )
    if not isinstance(created, dict):
        raise PublicationError("GitHub returned an unexpected PR payload")
    number = created.get("number")
    url = created.get("html_url")
    if not isinstance(number, int) or not isinstance(url, str):
        raise PublicationError("GitHub PR payload is missing number or html_url")
    returned_ref, returned_sha = _head_from_pr(created)
    if (
        returned_ref != request.head
        or returned_sha != request.expected_head
        or _base_from_pr(created) != request.base
        or _head_repository_from_pr(created) != repository_name
    ):
        _quarantine(runner, number=number)
        evidence_path = _write_evidence(
            evidence_dir or _repo_root() / ".audit" / "pr-publications",
            {
                "base": request.base,
                "expected_head": request.expected_head,
                "head": request.head,
                "pr": number,
                "review_task_id": request.review_task_id,
                "status": "returned_head_mismatch_quarantined",
                "url": url,
            },
        )
        raise PublicationError(
            f"PR #{number} returned a different head and was quarantined as closed; "
            f"evidence: {evidence_path}"
        )
    try:
        fresh = runner.run_json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{{owner}}/{{repo}}/pulls/{number}",
            ]
        )
    except PublicationError as exc:
        _quarantine(runner, number=number)
        raise PublicationError(
            f"PR #{number} could not be revalidated and was quarantined as closed"
        ) from exc
    if not isinstance(fresh, dict):
        _quarantine(runner, number=number)
        raise PublicationError(
            f"PR #{number} returned an unexpected fresh payload and was "
            "quarantined as closed"
        )
    fresh_ref, fresh_sha = _head_from_pr(fresh)
    if (
        fresh_ref != request.head
        or fresh_sha != request.expected_head
        or _base_from_pr(fresh) != request.base
        or _head_repository_from_pr(fresh) != repository_name
    ):
        _quarantine(runner, number=number)
        evidence_path = _write_evidence(
            evidence_dir or _repo_root() / ".audit" / "pr-publications",
            {
                "base": request.base,
                "expected_head": request.expected_head,
                "head": request.head,
                "pr": number,
                "review_task_id": request.review_task_id,
                "status": "head_mismatch_quarantined",
                "url": url,
            },
        )
        raise PublicationError(
            f"PR #{number} fresh head mismatched and was quarantined as closed; "
            f"evidence: {evidence_path}"
        )
    fresh_body = fresh.get("body")
    body_owner = "adopted" if adopted else "newly created"
    if not isinstance(fresh_body, str):
        if not adopted:
            _quarantine(runner, number=number)
        raise PublicationError(f"{body_owner} PR #{number} has no live body")
    if "standalone" in labels and not has_standalone_reason(fresh_body):
        if not adopted:
            _quarantine(runner, number=number)
        raise PublicationError(
            f"{body_owner} PR #{number} cannot receive the standalone label: its "
            "live body lacks a non-empty Standalone-Reason"
        )
    if error := validate_contract(fresh_body):
        if not adopted:
            _quarantine(runner, number=number)
        raise PublicationError(
            f"{body_owner} PR #{number} live body violates publication policy: "
            f"{error}"
        )
    if adopted:
        validate_adopted_body(
            request, number=number, body=fresh_body, error_type=PublicationError
        )

    metadata_mutations: list[dict[str, object]] = []
    if labels:
        mutation = {
            "kind": "batch_labels",
            "label_count": len(labels),
            "unavoidable": True,
        }
        try:
            runner.run_json(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    "--input",
                    "-",
                    f"repos/{{owner}}/{{repo}}/issues/{number}/labels",
                ],
                payload={"labels": list(labels)},
            )
        except PublicationError as exc:
            evidence_path = _write_evidence(
                evidence_dir or _repo_root() / ".audit" / "pr-publications",
                {
                    "base": request.base,
                    "body_repair_mutations": 0,
                    "head": request.head,
                    "labels": list(labels),
                    "post_open_metadata_mutations": [mutation | {"status": "failed"}],
                    "pr": number,
                    "status": "metadata_failed",
                    "url": url,
                },
            )
            raise PublicationError(
                f"PR #{number} opened, but its batched label mutation failed; "
                f"evidence: {evidence_path}"
            ) from exc
        metadata_mutations.append(mutation | {"status": "applied"})

    evidence_path = _write_published_evidence_once(
        evidence_dir or _repo_root() / ".audit" / "pr-publications",
        generation=request.review_reentry_generation,
        record={
            "base": request.base,
            "body_repair_mutations": 0,
            "head": request.head,
            "expected_head": request.expected_head,
            "labels": list(labels),
            "post_open_metadata_mutations": metadata_mutations,
            "pr": number,
            "review_task_id": request.review_task_id or None,
            "review_risk_json": request.review_risk_json,
            "review_risk_sha256": request.review_risk_sha256,
            "review_risk_tier": request.review_risk_tier,
            "admission_tier_floor": request.admission_tier_floor,
            "run_id": extract_run_id_marker(request.body),
            **({"review_exemption": "T0"} if not request.review_task_id else {}),
            **(
                {"patch_identity_carry": dict(request.patch_equivalence)}
                if request.patch_equivalence is not None
                else {}
            ),
            "status": "published",
            "adopted": adopted,
            "url": url,
        },
    )
    return PublicationResult(
        number=number,
        url=url,
        evidence_path=evidence_path,
        post_open_metadata_mutations=len(metadata_mutations),
    )
