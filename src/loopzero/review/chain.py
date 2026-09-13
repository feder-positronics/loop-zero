"""Single-chain review evidence contracts.

The outer Review Gate owns one primary capture per delivery source.  Security
is a required section of that capture when the already-canonical trigger
classifier reports matching paths; advisory passes can only append child
receipts linked to the immutable primary receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextvars import ContextVar

from ..config import Profile

REVIEW_CHAIN_RECEIPT_SCHEMA = "ReviewChainReceiptV1"
REVIEW_CHAIN_ADVISORY_RECEIPT_SCHEMA = "ReviewChainAdvisoryReceiptV1"
REVIEW_CHAIN_SECTIONS = ("code", "security")
_PROFILE: ContextVar[Profile | None] = ContextVar("review_chain_profile", default=None)
_DEFAULT_PROFILE: Profile | None = None


def configure(profile: Profile) -> None:
    global _DEFAULT_PROFILE
    _DEFAULT_PROFILE = profile
    _PROFILE.set(profile)


def _required_sections() -> tuple[str, ...]:
    profile = _PROFILE.get() or _DEFAULT_PROFILE
    if profile is None:
        return REVIEW_CHAIN_SECTIONS
    if not profile.required_sections:
        raise ReviewChainError("review chain requires configured required sections")
    return tuple(
        section for section in dict.fromkeys(profile.required_sections)
        if section != "security"
    )


def _required_for_paths(paths: Sequence[str]) -> tuple[str, ...]:
    configured = _required_sections()
    if paths and "security" not in configured:
        return (*configured, "security")
    return configured


def enforce_review_budget(*, completed_reviews: int, completed_delta_reviews: int,
                          requested: str) -> None:
    """Enforce the contract cap; consumer configuration may only narrow it."""
    profile = _PROFILE.get() or _DEFAULT_PROFILE
    if profile is None:
        raise ReviewChainError("review chain requires configured review budget")
    full_limit = min(profile.max_reviews_per_pr, 1)
    delta_limit = min(profile.max_delta_reviews, 1)
    if type(completed_reviews) is not int or type(completed_delta_reviews) is not int:
        raise ReviewChainError("review budget counters must be integers")
    if completed_reviews < 0 or completed_delta_reviews < 0:
        raise ReviewChainError("review budget counters cannot be negative")
    if requested == "review" and (
        completed_reviews >= full_limit or completed_delta_reviews != 0
    ):
        raise ReviewChainError("primary review budget exhausted")
    if requested == "delta" and completed_reviews != 1:
        raise ReviewChainError("delta review requires exactly one primary review")
    if requested == "delta" and completed_delta_reviews >= delta_limit:
        raise ReviewChainError("delta review budget exhausted")
    if requested not in {"review", "delta"}:
        raise ReviewChainError("unknown review budget kind")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ReviewChainError(ValueError):
    """Raised when review-chain evidence is incomplete or inconsistent."""


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def review_gate_key(payload: Mapping[str, object]) -> str:
    """Return the stable delivery-chain key; legacy lens labels are metadata."""
    contract = payload.get("task_contract")
    task = contract if isinstance(contract, Mapping) else payload
    intent = task.get("review_intent") or payload.get("review_intent")
    if intent == "delivery-code-review":
        return "delivery-chain"
    return str(task.get("category") or payload.get("category") or "")


def review_record_lens(payload: Mapping[str, object]) -> str:
    """Return the delta/finding section label, preserving historical records."""
    contract = payload.get("task_contract")
    task = contract if isinstance(contract, Mapping) else payload
    intent = task.get("review_intent") or payload.get("review_intent")
    if intent == "delivery-code-review":
        lens = task.get("review_lens") or payload.get("review_lens")
        if isinstance(lens, str) and lens:
            return lens
        category = str(task.get("category") or payload.get("category") or "")
        return "security" if "security" in category.lower() else "code"
    return str(task.get("category") or payload.get("category") or "")


def review_finding_lens(payload: Mapping[str, object], finding_id: str) -> str:
    """Return one finding's receipt-bound section, failing closed on ambiguity."""
    receipt = payload.get("review_chain_receipt")
    if not isinstance(receipt, Mapping):
        return review_record_lens(payload)
    required = receipt.get("required_sections")
    sections = receipt.get("sections")
    if (
        receipt.get("schema_version") != REVIEW_CHAIN_RECEIPT_SCHEMA
        or not isinstance(required, list)
        or not isinstance(sections, Mapping)
        or required not in (
            list(_required_for_paths(())),
            list(_required_for_paths(("security-trigger",))),
        )
        or set(sections) != set(required)
    ):
        return ""
    matches: list[str] = []
    seen_finding_ids: set[str] = set()
    for section in required:
        evidence = sections.get(section)
        finding_ids = (
            evidence.get("finding_ids") if isinstance(evidence, Mapping) else None
        )
        if not isinstance(finding_ids, list) or not all(
            isinstance(value, str) and value for value in finding_ids
        ):
            return ""
        if any(value in seen_finding_ids for value in finding_ids) or len(
            set(finding_ids)
        ) != len(finding_ids):
            return ""
        seen_finding_ids.update(finding_ids)
        if finding_id in finding_ids:
            matches.append(str(section))
    return matches[0] if len(matches) == 1 else ""


def validate_review_chain_task(task: Mapping[str, object]) -> tuple[str, ...]:
    """Validate the hash-bound closed section set selected by the outer gate."""
    raw_paths = task.get("security_trigger_paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path for path in raw_paths
    ):
        raise ReviewChainError("security_trigger_paths must be a string list")
    if raw_paths != sorted(set(raw_paths)):
        raise ReviewChainError("security_trigger_paths must be sorted and unique")
    expected = _required_for_paths(raw_paths)
    raw_sections = task.get("required_sections")
    if not isinstance(raw_sections, list) or tuple(raw_sections) != expected:
        raise ReviewChainError(
            "required_sections must exactly match the security trigger classifier"
        )
    return expected


def review_sections_schema(
    required_sections: Sequence[str],
    *,
    finding_schema: Mapping[str, object],
    bounded_string: Mapping[str, object],
) -> dict[str, object]:
    """Build the strict model-output fragment for one primary review chain."""
    properties: dict[str, object] = {}
    for section in required_sections:
        section_properties: dict[str, object] = {
            "completion": {"type": "string", "const": "completed"},
            "verdict": {"type": "string", "enum": ["clean", "findings"]},
            "findings": {
                "type": "array",
                "maxItems": 64,
                "items": dict(finding_schema),
            },
        }
        required = ["completion", "verdict", "findings"]
        if section == "security":
            section_properties["threat_model_summary"] = dict(bounded_string)
            required.append("threat_model_summary")
        properties[section] = {
            "type": "object",
            "properties": section_properties,
            "required": required,
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(required_sections),
        "additionalProperties": False,
    }


def validate_review_chain_result(
    result: Mapping[str, object], *, task: Mapping[str, object]
) -> list[dict[str, object]]:
    """Return classified findings or reject incomplete section evidence."""
    required = validate_review_chain_task(task)
    raw_sections = result.get("review_sections")
    if not isinstance(raw_sections, Mapping) or tuple(raw_sections) != required:
        raise ReviewChainError("review sections must exactly match required_sections")
    classified: list[dict[str, object]] = []
    for section in required:
        raw = raw_sections.get(section)
        if not isinstance(raw, Mapping):
            raise ReviewChainError(f"review section {section!r} is missing")
        expected_fields = {"completion", "verdict", "findings"}
        if section == "security":
            expected_fields.add("threat_model_summary")
            threat_model = raw.get("threat_model_summary")
            if not isinstance(threat_model, str) or not threat_model.strip():
                raise ReviewChainError(
                    "security section requires a non-empty threat-model summary"
                )
        if set(raw) != expected_fields or raw.get("completion") != "completed":
            raise ReviewChainError(f"review section {section!r} is incomplete")
        findings = raw.get("findings")
        if not isinstance(findings, list) or not all(
            isinstance(finding, dict) for finding in findings
        ):
            raise ReviewChainError(f"review section {section!r} findings are invalid")
        expected_verdict = "findings" if findings else "clean"
        if raw.get("verdict") != expected_verdict:
            raise ReviewChainError(
                f"review section {section!r} verdict does not match its findings"
            )
        classified.extend(findings)
    if result.get("findings") != classified:
        raise ReviewChainError(
            "top-level findings contain unclassified or reordered section findings"
        )
    return classified


def _chain_id(
    *, task_id: str, snapshot_tree_sha: str, patch_identity: Mapping[str, object] | None
) -> str:
    return (
        "rc_"
        + _canonical_sha256(
            {
                "task_id": task_id,
                "snapshot_tree_sha": snapshot_tree_sha,
                "patch_identity": patch_identity,
            }
        )[:32]
    )


def build_review_chain_receipt(
    *,
    task: Mapping[str, object],
    snapshot_sha: str,
    snapshot_tree_sha: str,
    patch_identity: Mapping[str, object] | None,
    result: Mapping[str, object],
    finding_ids: Sequence[str],
) -> dict[str, object]:
    """Build the immutable primary receipt after findings are deposited."""
    from .evidence import persisted_review_findings

    required = validate_review_chain_task(task)
    # Mirror the deposit default so a contract without review_intent keeps the
    # receipt and the ledger selection in agreement instead of raising KeyError.
    review_intent = str(task.get("review_intent") or "discovery")
    classified = persisted_review_findings(
        validate_review_chain_result(result, task=task), review_intent=review_intent
    )
    if len(finding_ids) != len(classified) or not all(
        isinstance(finding_id, str) and finding_id for finding_id in finding_ids
    ) or len(set(finding_ids)) != len(finding_ids):
        raise ReviewChainError("review-chain finding IDs do not match findings")
    if not _SHA40.fullmatch(snapshot_sha) or not _SHA40.fullmatch(snapshot_tree_sha):
        raise ReviewChainError("review-chain snapshot identity is invalid")
    offset = 0
    sections: dict[str, object] = {}
    raw_sections = result["review_sections"]
    assert isinstance(raw_sections, Mapping)
    for section in required:
        section_result = raw_sections[section]
        assert isinstance(section_result, Mapping)
        section_findings = section_result["findings"]
        assert isinstance(section_findings, list)
        count = len(
            persisted_review_findings(section_findings, review_intent=review_intent)
        )
        section_receipt: dict[str, object] = {
            "completion": section_result["completion"],
            "verdict": section_result["verdict"],
            "finding_ids": list(finding_ids[offset : offset + count]),
        }
        if section == "security":
            section_receipt["threat_model_summary"] = section_result[
                "threat_model_summary"
            ]
        sections[section] = section_receipt
        offset += count
    paths = list(task["security_trigger_paths"])
    task_id = str(task.get("task_id") or "")
    return {
        "schema_version": REVIEW_CHAIN_RECEIPT_SCHEMA,
        "chain_id": _chain_id(
            task_id=task_id,
            snapshot_tree_sha=snapshot_tree_sha,
            patch_identity=patch_identity,
        ),
        "task_id": task_id,
        "snapshot_sha": snapshot_sha,
        "snapshot_tree_sha": snapshot_tree_sha,
        "patch_identity": dict(patch_identity) if patch_identity is not None else None,
        "trigger_classifier": {
            "security_trigger_paths": paths,
            "security_required": bool(paths),
        },
        "required_sections": list(required),
        "sections": sections,
    }


def review_chain_receipt_reasons(
    receipt: Mapping[str, object],
    *,
    task: Mapping[str, object],
    snapshot_sha: str,
    snapshot_tree_sha: str,
    patch_identity: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Return fail-closed primary receipt defects without raising."""
    reasons: list[str] = []
    try:
        required = validate_review_chain_task(task)
    except ReviewChainError:
        return ("invalid-review-chain-task",)
    if receipt.get("schema_version") != REVIEW_CHAIN_RECEIPT_SCHEMA:
        reasons.append("schema-mismatch")
    if receipt.get("task_id") != task.get("task_id"):
        reasons.append("task-mismatch")
    if receipt.get("snapshot_sha") != snapshot_sha:
        reasons.append("snapshot-mismatch")
    if receipt.get("snapshot_tree_sha") != snapshot_tree_sha:
        reasons.append("snapshot-tree-mismatch")
    if receipt.get("patch_identity") != patch_identity:
        reasons.append("patch-identity-mismatch")
    expected_chain = _chain_id(
        task_id=str(task.get("task_id") or ""),
        snapshot_tree_sha=snapshot_tree_sha,
        patch_identity=patch_identity,
    )
    if receipt.get("chain_id") != expected_chain:
        reasons.append("chain-id-mismatch")
    paths = task.get("security_trigger_paths")
    expected_classifier = {
        "security_trigger_paths": paths,
        "security_required": bool(paths),
    }
    if receipt.get("trigger_classifier") != expected_classifier:
        reasons.append("trigger-classifier-mismatch")
    if receipt.get("required_sections") != list(required):
        reasons.append("required-sections-mismatch")
    sections = receipt.get("sections")
    if not isinstance(sections, Mapping) or tuple(sections) != required:
        reasons.append("section-evidence-mismatch")
    else:
        seen_finding_ids: set[str] = set()
        for section in required:
            evidence = sections.get(section)
            if not isinstance(evidence, Mapping):
                reasons.append("section-evidence-mismatch")
                break
            expected_fields = {"completion", "verdict", "finding_ids"}
            if section == "security":
                expected_fields.add("threat_model_summary")
            if (
                set(evidence) != expected_fields
                or evidence.get("completion") != "completed"
                or evidence.get("verdict") not in {"clean", "findings"}
                or not isinstance(evidence.get("finding_ids"), list)
            ):
                reasons.append("section-evidence-mismatch")
                break
            finding_ids = evidence["finding_ids"]
            assert isinstance(finding_ids, list)
            if (
                not all(
                    isinstance(finding_id, str) and finding_id
                    for finding_id in finding_ids
                )
                or len(set(finding_ids)) != len(finding_ids)
                or any(finding_id in seen_finding_ids for finding_id in finding_ids)
            ):
                reasons.append("duplicate-or-invalid-finding-id")
                break
            seen_finding_ids.update(finding_ids)
            if section == "security" and (
                not isinstance(evidence.get("threat_model_summary"), str)
                or not str(evidence.get("threat_model_summary")).strip()
            ):
                reasons.append("security-threat-model-missing")
                break
    return tuple(dict.fromkeys(reasons))


def build_review_chain_advisory_receipt(
    *,
    primary_receipt: Mapping[str, object],
    advisory_task_id: str,
    request_sha256: str,
    result_sha256: str,
    finding_ids: Sequence[str],
) -> dict[str, object]:
    """Build an append-only advisory child with no primary-section authority."""
    chain_id = primary_receipt.get("chain_id")
    tree = primary_receipt.get("snapshot_tree_sha")
    if (
        primary_receipt.get("schema_version") != REVIEW_CHAIN_RECEIPT_SCHEMA
        or not isinstance(chain_id, str)
        or not chain_id
        or not isinstance(tree, str)
        or not _SHA40.fullmatch(tree)
        or not advisory_task_id
        or not _SHA256.fullmatch(request_sha256)
        or not _SHA256.fullmatch(result_sha256)
        or not all(
            isinstance(finding_id, str) and finding_id for finding_id in finding_ids
        )
    ):
        raise ReviewChainError("advisory review-chain evidence is invalid")
    return {
        "schema_version": REVIEW_CHAIN_ADVISORY_RECEIPT_SCHEMA,
        "chain_id": chain_id,
        "primary_task_id": primary_receipt.get("task_id"),
        "advisory_task_id": advisory_task_id,
        "snapshot_tree_sha": tree,
        "request_sha256": request_sha256,
        "result_sha256": result_sha256,
        "finding_ids": list(finding_ids),
    }


# Tree-coverage predicates are exposed from this target module while kept in a
# private source file to avoid an import cycle during receipt construction.
from ._tree_coverage import (  # noqa: E402
    accepted_review_patch_identity,
    carried_review_patch_identity,
    chain_covers_rebased_tree,
    chain_covers_tree,
    covers_frozen_tree,
    load_delta_edges,
    mechanical_review_carry,
    review_record_ref,
    review_task_covers_tree,
    section_patch_equivalence,
    terminal_has_review_section,
)
