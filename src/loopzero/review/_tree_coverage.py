"""Shared exact-tree publication review coverage predicates."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ..kernel.patch_identity import (
    PatchIdentityError,
    compute_patch_identity,
    capture_patch_identity,
    prove_clean_head_carry,
    prove_patch_equivalence,
    prove_format_only,
    validate_patch_carry,
)
from .chain import review_finding_lens, review_record_lens as review_record_lens
from .risk import (
    SecurityReviewScopeError,
    security_trigger_paths_between,
)
from ..kernel.worktree_lease import identities_match
from ..kernel.run_log import load_entries
from ..kernel.run_identity import run_delivery_contract
from ..kernel.gitscope import primary_repo_root
from ..kernel import settings as kernel_settings

_TREE_SHA_RE = re.compile(r"[0-9a-f]{40}")


class CommandRunner(Protocol):
    def run(self, args: list[str], *, check: bool = True) -> object: ...


def mechanical_review_carry(
    repo: Path, terminal: Mapping[str, object] | None, current_tree: str
) -> bool:
    """Permit only mechanically proven edits for an original loop-zero owner."""
    if terminal is None or not isinstance(terminal.get("run_id"), str):
        return False
    rows = load_entries(primary_repo_root(repo) / kernel_settings.settings.audit_root / "skill-runs")
    run_id = str(terminal["run_id"])
    if not any(row.get("run_id") == run_id for row in rows):
        return False
    if run_delivery_contract(rows, run_id) != "loop-zero-v1":
        return False
    return prove_format_only(
        repo, str(terminal.get("snapshot_tree_sha") or ""), current_tree
    )


def review_record_ref(record: Mapping[str, object]) -> str:
    identity = record.get("source_identity")
    return str(identity.get("ref") or "") if isinstance(identity, Mapping) else ""


def terminal_has_review_section(record: Mapping[str, object], section: str) -> bool:
    """Return whether hash-bound delivery evidence contains one review section."""
    contract = record.get("task_contract")
    chain_receipt = record.get("review_chain_receipt")
    if isinstance(contract, Mapping) and isinstance(chain_receipt, Mapping):
        return (
            record.get("review_intent") == "delivery-code-review"
            and contract.get("review_intent") == "delivery-code-review"
            and section in chain_receipt.get("required_sections", [])
            and section in chain_receipt.get("sections", {})
        )
    return (
        record.get("review_intent") == "delivery-code-review"
        and record.get("review_lens") == section
        and isinstance(contract, Mapping)
        and contract.get("review_intent") == "delivery-code-review"
        and contract.get("review_lens") == section
    )


def accepted_review_patch_identity(
    terminal: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Return a persisted review identity bound to its accepted terminal."""
    identity = terminal.get("patch_identity") if terminal is not None else None
    source = terminal.get("source_identity") if terminal is not None else None
    source_head = source.get("head") if isinstance(source, Mapping) else None
    snapshot_sha = terminal.get("snapshot_sha") if terminal is not None else None
    bound_candidates = {
        candidate
        for candidate in (source_head, snapshot_sha)
        if isinstance(candidate, str) and _TREE_SHA_RE.fullmatch(candidate)
    }
    if (
        not isinstance(identity, Mapping)
        or not isinstance(source, Mapping)
        or identity.get("candidate_sha") not in bound_candidates
        or identity.get("candidate_tree_sha") != terminal.get("snapshot_tree_sha")
    ):
        return None
    return identity


def carried_review_patch_identity(
    repo: Path,
    records: Sequence[Mapping[str, object]],
    terminal: Mapping[str, object] | None,
    task_id: str,
    expected_head: str,
    *,
    current_source: Mapping[str, object] | None = None,
    patch_carry_validator: Callable[
        [Path, Mapping[str, object], Mapping[str, object]],
        tuple[dict[str, object], dict[str, object], dict[str, object]] | None,
    ] = validate_patch_carry,
) -> Mapping[str, object] | None:
    """Return one validated captured identity for the exact publication head."""
    original = accepted_review_patch_identity(terminal)
    if terminal is None or original is None:
        return None
    source = terminal.get("source_identity")
    if current_source is not None and (
        not isinstance(source, Mapping)
        or source.get("ref") != current_source.get("ref")
        or current_source.get("head") != expected_head
    ):
        return None
    if (
        isinstance(source, Mapping)
        and source.get("head") == expected_head
        and original.get("candidate_sha") == expected_head
    ):
        return original
    identities: dict[str, Mapping[str, object]] = {}
    for record in records:
        if record.get("task_id") != task_id:
            continue
        validated = patch_carry_validator(repo, terminal, record)
        if validated is None or validated[0].get("head") != expected_head:
            continue
        identity = validated[1]
        identities[json.dumps(identity, sort_keys=True, separators=(",", ":"))] = (
            identity
        )
    if identities:
        return next(iter(identities.values())) if len(identities) == 1 else None
    # Publication already holds the exact clean source lease. A rebase need not
    # have visited the dispatcher to persist a carry: recompute the same proof
    # from the accepted terminal and immutable candidate objects here.
    if current_source is not None:
        carried = prove_clean_head_carry(repo, original, current_head=expected_head)
        if carried is not None:
            return carried[0]
    return None


def section_patch_equivalence(
    repo: Path,
    review_identity: Mapping[str, object],
    current_identity: Mapping[str, object],
    *,
    lens: str,
    runner: CommandRunner | None = None,
) -> Mapping[str, object] | None:
    """Carry author-identical content only across security-neutral base motion."""
    proof = prove_patch_equivalence(repo, review_identity, current_identity)
    if proof is not None and lens == "security":
        try:
            overlap = set(proof["base_path_overlap"])
            for identity in (review_identity, current_identity):
                if overlap and overlap.intersection(
                    security_trigger_paths_between(
                        repo,
                        str(identity["base_sha"]),
                        str(identity["candidate_sha"]),
                        runner=runner,
                    )
                ):
                    return None
            if security_trigger_paths_between(
                repo,
                str(review_identity["candidate_tree_sha"]),
                str(current_identity["candidate_tree_sha"]),
                runner=runner,
            ):
                return None
        except SecurityReviewScopeError:
            return None
    return proof


def _verdict_value(value: object) -> str:
    if isinstance(value, Mapping):
        return str(value.get("verdict") or "")
    return str(value or "")


def load_delta_edges(
    repo: Path,
    *,
    lens: str,
    source_ref: str,
    accepted_terminals: Mapping[str, Mapping[str, object]],
    latest_verdicts: Mapping[str, object],
    finding_records: Sequence[Mapping[str, object]],
    runner: CommandRunner | None = None,
    security_classifier: Callable[
        ..., tuple[str, ...]
    ] = security_trigger_paths_between,
) -> dict[str, str]:
    """Map exact reviewed or proven tree transitions for one standing lens."""
    edges: dict[str, str] = {}
    for task_id, terminal in accepted_terminals.items():
        source_tree = terminal.get("delta_from_tree_sha")
        target_tree = terminal.get("snapshot_tree_sha")
        if (
            review_record_ref(terminal) != source_ref
            or _verdict_value(latest_verdicts.get(task_id)) != "pass"
            or not isinstance(source_tree, str)
            or not isinstance(target_tree, str)
        ):
            continue
        covers_lens = terminal_has_review_section(terminal, lens)
        if (
            not covers_lens
            and lens == "security"
            and terminal_has_review_section(terminal, "code")
            and _TREE_SHA_RE.fullmatch(source_tree) is not None
            and _TREE_SHA_RE.fullmatch(target_tree) is not None
            and not security_classifier(
                repo,
                source_tree,
                target_tree,
                runner=runner,
            )
        ):
            covers_lens = True
        if covers_lens:
            edges[source_tree] = target_tree

    evidence_transitions: dict[tuple[str, str], set[str]] = {}
    for finding in finding_records:
        evidence = finding.get("deterministic_evidence")
        review_task_id = finding.get("review_task_id")
        terminal = (
            accepted_terminals.get(review_task_id)
            if isinstance(review_task_id, str)
            else None
        )
        finding_id = finding.get("finding_id")
        origin_lens = (
            review_finding_lens(terminal, finding_id)
            if terminal is not None and isinstance(finding_id, str)
            else ""
        )
        if (
            finding.get("state") != "addressed"
            or not isinstance(evidence, Mapping)
            or terminal is None
            or _verdict_value(latest_verdicts.get(str(review_task_id))) != "pass"
            or not terminal_has_review_section(terminal, origin_lens)
            or review_record_ref(terminal) != source_ref
        ):
            continue
        source_tree = finding.get("snapshot_tree_sha")
        target_tree = evidence.get("target_tree_sha")
        if (
            not isinstance(source_tree, str)
            or not isinstance(target_tree, str)
            or _TREE_SHA_RE.fullmatch(source_tree) is None
            or _TREE_SHA_RE.fullmatch(target_tree) is None
        ):
            continue
        evidence_transitions.setdefault((source_tree, target_tree), set()).add(
            origin_lens
        )
    for (source_tree, target_tree), origin_lenses in evidence_transitions.items():
        if lens not in origin_lenses:
            if lens not in {"code", "security"} or not origin_lenses <= {
                "code",
                "security",
            }:
                continue
        if (
            lens == "security"
            and "code" in origin_lenses
            and security_classifier(
                repo,
                source_tree,
                target_tree,
                runner=runner,
            )
        ):
            continue
        edges[source_tree] = target_tree
    return edges


def chain_covers_tree(
    start_tree: str | None,
    edges: Mapping[str, str],
    target_tree: str | None,
    *,
    max_hops: int = 64,
) -> bool:
    """Walk delta edges from a full review's tree toward the target tree."""
    if not start_tree or not target_tree:
        return False
    tree = start_tree
    for _ in range(max_hops):
        if tree == target_tree:
            return True
        next_tree = edges.get(tree)
        if next_tree is None or next_tree == tree:
            return False
        tree = next_tree
    return False


def covers_frozen_tree(
    *,
    review_snapshot_tree: str | None,
    current_tree: str | None,
    chain_covered: bool,
    review_source: Mapping[str, object],
    current_source: Mapping[str, object],
    patch_equivalence: Mapping[str, object] | None,
) -> bool:
    """Canonical publication predicate for one accepted review section."""
    return bool(
        review_snapshot_tree == current_tree
        or chain_covered
        or identities_match(review_source, current_source)
        or patch_equivalence is not None
    )


def chain_covers_rebased_tree(
    repo: Path,
    *,
    start_tree: str | None,
    edges: Mapping[str, str],
    current_head: str,
    current_tree: str,
    lens: str,
    accepted_terminals: Mapping[str, Mapping[str, object]],
    latest_verdicts: Mapping[str, object],
    source_ref: str,
    finding_records: Sequence[Mapping[str, object]],
    runner: CommandRunner | None = None,
) -> bool:
    """Compose accepted repair coverage with a separate exact rebase proof."""
    if not edges:
        return False
    current = capture_patch_identity(repo, candidate_sha=current_head)
    if current is None or current["candidate_tree_sha"] != current_tree:
        return False
    # Only accepted finding repairs supply a new replay endpoint. Bind its
    # patch to the originating review's recorded base, never a base inferred
    # from an unrelated same-tree snapshot or later origin/main ancestry.
    for finding in finding_records:
        evidence = finding.get("deterministic_evidence")
        task_id = str(finding.get("review_task_id") or "")
        terminal = accepted_terminals.get(task_id)
        if (
            finding.get("state") != "addressed"
            or not isinstance(evidence, Mapping)
            or terminal is None
            or review_record_ref(terminal) != source_ref
            or _verdict_value(latest_verdicts.get(task_id)) != "pass"
        ):
            continue
        identity = accepted_review_patch_identity(terminal)
        snapshot = evidence.get("target_snapshot_sha")
        tree = evidence.get("target_tree_sha")
        source_tree = finding.get("snapshot_tree_sha")
        if (
            identity is None
            or not isinstance(snapshot, str)
            or _TREE_SHA_RE.fullmatch(snapshot) is None
            or not isinstance(tree, str)
            or not isinstance(source_tree, str)
            or edges.get(source_tree) != tree
            or not chain_covers_tree(start_tree, edges, tree)
        ):
            continue
        try:
            accepted = compute_patch_identity(
                repo, base_sha=str(identity.get("base_sha")), candidate_sha=snapshot
            )
        except (PatchIdentityError, OSError, UnicodeError):
            continue
        if (
            accepted is not None
            and accepted["candidate_tree_sha"] == tree
            and section_patch_equivalence(
                repo, accepted, current, lens=lens, runner=runner
            ) is not None
        ):
            return True
    return False


def review_task_covers_tree(
    repo: Path,
    *,
    records: Sequence[Mapping[str, object]],
    accepted_terminals: Mapping[str, Mapping[str, object]],
    latest_verdicts: Mapping[str, object],
    finding_records: Sequence[Mapping[str, object]],
    task_id: str,
    lens: str,
    current_source: Mapping[str, object],
    current_tree: str,
    runner: CommandRunner | None = None,
) -> bool:
    """Evaluate one accepted task with the exact publication coverage predicate."""
    terminal = accepted_terminals.get(task_id)
    if (
        terminal is None
        or _verdict_value(latest_verdicts.get(task_id)) != "pass"
        or not terminal_has_review_section(terminal, lens)
    ):
        return False
    source = terminal.get("source_identity")
    snapshot_tree = terminal.get("snapshot_tree_sha")
    current_head = current_source.get("head")
    if (
        not isinstance(source, Mapping)
        or source.get("version") != 2
        or not isinstance(snapshot_tree, str)
        or not isinstance(current_head, str)
        or source.get("ref") != current_source.get("ref")
    ):
        return False
    review_identity = accepted_review_patch_identity(terminal)
    current_identity = carried_review_patch_identity(
        repo,
        records,
        terminal,
        task_id,
        current_head,
        current_source=current_source,
    )
    equivalence = (
        section_patch_equivalence(
            repo, review_identity, current_identity, lens=lens, runner=runner
        )
        if review_identity is not None and current_identity is not None
        else None
    )
    edges = load_delta_edges(
        repo,
        lens=lens,
        source_ref=review_record_ref(terminal),
        accepted_terminals=accepted_terminals,
        latest_verdicts=latest_verdicts,
        finding_records=finding_records,
        runner=runner,
    )
    return covers_frozen_tree(
        review_snapshot_tree=snapshot_tree,
        current_tree=current_tree,
        chain_covered=mechanical_review_carry(repo, terminal, current_tree)
        or chain_covers_tree(snapshot_tree, edges, current_tree)
        or chain_covers_rebased_tree(
            repo,
            start_tree=snapshot_tree,
            edges=edges,
            current_head=current_head,
            current_tree=current_tree,
            lens=lens,
            accepted_terminals=accepted_terminals,
            latest_verdicts=latest_verdicts,
            source_ref=review_record_ref(terminal),
            finding_records=finding_records,
            runner=runner,
        ),
        review_source=source,
        current_source=current_source,
        patch_equivalence=equivalence,
    )
