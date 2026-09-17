"""PR-bound review facts derived from existing authenticated result artifacts.

No mutable finding state lives here. Registration verifies remote identity;
readiness reads admitted terminals and their exact result bytes each time.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..kernel.gitscope import DispatchError
from .evidence import (
    MAX_RESULT_ITEMS,
    MAX_RESULT_STRING,
    _finding_capture_id,
    _validate_result_findings,
)

DIGEST = re.compile(r"[0-9a-f]{64}\Z")
FINDING_ID = re.compile(r"f_[0-9a-f]{20}\Z")


def disposition_obligation(
    task: Mapping[str, object],
) -> tuple[str, tuple[str, ...]] | None:
    """Validate obligations carried inside the signed delta task contract."""
    if not ({"primary_result_sha256", "required_finding_ids"} & task.keys()):
        return None
    digest, ids = task.get("primary_result_sha256"), task.get("required_finding_ids")
    if (
        task.get("review_intent") != "delivery-code-review"
        or not isinstance(digest, str)
        or not DIGEST.fullmatch(digest)
        or not isinstance(ids, list)
        or len(ids) > MAX_RESULT_ITEMS
        or any(
            not isinstance(value, str) or not FINDING_ID.fullmatch(value)
            for value in ids
        )
        or len(ids) != len(set(ids))
    ):
        raise DispatchError("invalid authenticated delta disposition obligation")
    return digest, tuple(sorted(ids))


def validate_dispositions(
    result: Mapping[str, object], task: Mapping[str, object]
) -> None:
    obligation = disposition_obligation(task)
    if obligation is None:
        if {"dispositions", "primary_result_sha256"} & result.keys():
            raise DispatchError("dispositions require an admitted delta obligation")
        return
    digest, ids = obligation
    rows = result.get("dispositions")
    if result.get("primary_result_sha256") != digest or not isinstance(rows, list):
        raise DispatchError("delta must name the exact primary and all dispositions")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "finding_id",
            "outcome",
            "rationale",
        }:
            raise DispatchError("invalid delta disposition fields")
        identifier = row["finding_id"]
        rationale = row["rationale"]
        if (
            not isinstance(identifier, str)
            or identifier not in ids
            or identifier in seen
            or row["outcome"] not in {"repaired", "rejected", "unresolved"}
            or not isinstance(rationale, str)
            or not rationale.strip()
            or len(rationale) > MAX_RESULT_STRING
        ):
            raise DispatchError("invalid, duplicate or unsupported delta disposition")
        seen.add(identifier)
    if seen != set(ids):
        raise DispatchError("delta does not disposition every required finding")


def disposition_schema(task: Mapping[str, object]) -> dict[str, object]:
    digest, ids = disposition_obligation(task) or (None, ())
    if digest is None:
        return {}
    return {
        "primary_result_sha256": {"type": "string", "const": digest},
        "dispositions": {
            "type": "array",
            "minItems": len(ids),
            "maxItems": len(ids),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "finding_id": (
                        {"type": "string", "enum": list(ids)}
                        if ids
                        else {"type": "string"}
                    ),
                    "outcome": {
                        "type": "string",
                        "enum": ["repaired", "rejected", "unresolved"],
                    },
                    "rationale": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_RESULT_STRING,
                    },
                },
                "required": ["finding_id", "outcome", "rationale"],
            },
        },
    }


def material_findings(
    tree: str, result: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    """Use existing stable IDs; suggestions do not become blocking state."""
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise DispatchError("review findings require a source tree")
    return {
        _finding_capture_id(tree, row): row
        for row in _validate_result_findings(result.get("findings"))
        if row["severity"] in {"critical", "important"}
    }


def verify_pr_identity(
    runner,
    *,
    repository: str,
    pr: int,
    run_id: str,
    head: str,
    branch: str,
    base: str,
    base_sha: str,
) -> dict[str, object]:
    """Read live GitHub identity using the host's trusted runner, before signing."""
    from ..kernel.run_identity import extract_run_id_marker

    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or type(pr) is not int
        or pr < 1
        or not re.fullmatch(r"sr_[0-9a-f]{32}", run_id)
        or any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, base_sha))
    ):
        raise DispatchError("invalid PR review identity")
    owned_repository = runner.json(
        ["gh", "api", "--method", "GET", "repos/{owner}/{repo}"]
    )
    if (
        not isinstance(owned_repository, dict)
        or owned_repository.get("full_name") != repository
    ):
        raise DispatchError("PR review repository differs from trusted checkout")
    payload = runner.json(
        ["gh", "api", "--method", "GET", f"repos/{repository}/pulls/{pr}"]
    )
    if not isinstance(payload, dict):
        raise DispatchError("PR review identity unavailable")
    live_head, live_base = payload.get("head"), payload.get("base")
    if not isinstance(live_head, dict) or not isinstance(live_base, dict):
        raise DispatchError("PR review source unavailable")
    if (
        payload.get("number") != pr
        or payload.get("state") != "open"
        or extract_run_id_marker(str(payload.get("body", ""))) != run_id
        or live_head.get("sha") != head
        or live_head.get("ref") != branch
        or live_base.get("sha") != base_sha
        or live_base.get("ref") != base
        or not isinstance(live_head.get("repo"), dict)
        or not isinstance(live_base.get("repo"), dict)
        or live_head["repo"].get("full_name") != repository
        or live_base["repo"].get("full_name") != repository
    ):
        raise DispatchError("PR review identity differs from owned source")
    return {
        "repository": repository,
        "pr": pr,
        "run_id": run_id,
        "head": head,
        "branch": branch,
        "base": base,
        "base_sha": base_sha,
    }


def authenticated_result(
    repo: Path,
    records: Sequence[dict[str, object]],
    task_id: str,
    *,
    pr_identity: Mapping[str, object],
) -> tuple[dict, dict]:
    """Require actual accepted registered terminals, never a supplied verdict map."""
    from ..kernel.authority_projection import (
        _authenticated_attempt_terminal_registrations,
    )
    from .authority import (
        _result_artifact_bytes,
        accepted_review_terminals,
        normalize_review_result,
    )

    terminal = accepted_review_terminals(records).get(task_id)
    if terminal is None or terminal.get("status") != "completed":
        raise DispatchError("PR review has no authenticated accepted terminal")
    registration = _authenticated_attempt_terminal_registrations(records).get(
        id(terminal)
    )
    contract = terminal.get("task_contract")
    if not isinstance(registration, Mapping) or not isinstance(contract, Mapping):
        raise DispatchError("PR review registration is missing")
    if (
        registration.get("task_contract") != contract
        or contract.get("pr_identity") != dict(pr_identity)
        or terminal.get("run_id") != pr_identity.get("run_id")
        or terminal.get("source_identity", {}).get("head") != pr_identity.get("head")
    ):
        raise DispatchError("PR review registration/source mismatch")
    raw = _result_artifact_bytes(repo, terminal.get("result_artifact"))
    if hashlib.sha256(raw).hexdigest() != terminal.get("result_sha256"):
        raise DispatchError("PR review result digest changed")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DispatchError("PR review result is invalid") from exc
    return terminal, normalize_review_result(result, task=contract)


def _require_consumed_role(records, terminal, role):
    """A generation's inherited consumption is not this terminal's slot role."""
    from ..kernel.authority_projection import (
        authenticated_review_state_records,
        slot_state,
    )
    from ..kernel.review_state import ReviewSlotReservation, canonical_record_digest
    from ..runners.contract import ReviewOutcome

    for row in authenticated_review_state_records(records):
        if row.get("reservation_id") != terminal.get("review_reservation_id"):
            continue
        try:
            reservation = ReviewSlotReservation.from_dict(row)
        except TypeError, ValueError:
            continue
        if (
            reservation.slot_kind == role
            and reservation.task_id == terminal.get("task_id")
            and reservation.generation_id == terminal.get("review_generation_id")
        ):
            state = slot_state(records, reservation.generation_id, "delivery")
            settlement = state.settlement_for(reservation.reservation_id)
            if (
                settlement is not None
                and settlement.terminal_ref == canonical_record_digest(terminal)
                and state._effective_outcome(settlement) is ReviewOutcome.CONSUMED
            ):
                return
    raise DispatchError(f"review lacks authenticated consumed {role} reservation")


def _require_committed_source(worktree, expected, *, reviewed_tree=None):
    """PR review must describe the clean committed tree actually on the PR."""
    from ..kernel.worktree_lease import _git_bytes, _git_text, source_identity

    current = source_identity(worktree)
    if current != dict(expected):
        raise DispatchError("current source differs from reviewed source")
    if _git_bytes(worktree, "status", "--porcelain=v2", "-z", "-uall"):
        raise DispatchError("PR review requires clean committed source")
    tree = _git_text(worktree, "rev-parse", "HEAD^{tree}")
    if reviewed_tree is not None and tree != reviewed_tree:
        raise DispatchError("PR review tree differs from committed source")
    if source_identity(worktree) != current:
        raise DispatchError("PR review source changed during verification")


def delta_obligations(
    repo: Path,
    records: Sequence[dict[str, object]],
    primary_task_id: str,
    *,
    pr_identity: Mapping[str, object],
) -> dict[str, object]:
    terminal, result = authenticated_result(
        repo, records, primary_task_id, pr_identity=pr_identity
    )
    return {
        "primary_result_sha256": terminal["result_sha256"],
        "required_finding_ids": sorted(
            material_findings(terminal["snapshot_tree_sha"], result)
        ),
    }


def blocking_findings(
    repo: Path,
    records: Sequence[dict[str, object]],
    *,
    primary_task_id: str,
    current_identity: Mapping[str, object],
    delta_task_id: str | None = None,
) -> dict[str, dict[str, object]]:
    """Compute blocking facts; a clean delta never silently closes its parent."""
    from .authority import accepted_review_terminals

    primary = accepted_review_terminals(records).get(primary_task_id)
    if primary is None:
        raise DispatchError("primary review is not authenticated")
    primary_identity = primary.get("task_contract", {}).get("pr_identity")
    if not isinstance(primary_identity, dict):
        raise DispatchError("primary review has no registered PR")
    stable = ("repository", "pr", "run_id", "branch", "base", "base_sha")
    if any(primary_identity.get(key) != current_identity.get(key) for key in stable):
        raise DispatchError("primary review belongs to another PR/base")
    primary, result = authenticated_result(
        repo, records, primary_task_id, pr_identity=primary_identity
    )
    _require_consumed_role(records, primary, "primary")
    open_findings = material_findings(primary["snapshot_tree_sha"], result)
    if delta_task_id is None:
        if primary_identity != dict(current_identity):
            raise DispatchError("changed source requires an admitted delta")
        return open_findings
    if delta_task_id == primary_task_id:
        raise DispatchError("primary cannot serve as its own delta")
    delta, delta_result = authenticated_result(
        repo, records, delta_task_id, pr_identity=current_identity
    )
    _require_consumed_role(records, delta, "delta")
    contract = delta["task_contract"]
    expected = {
        "primary_result_sha256": primary["result_sha256"],
        "required_finding_ids": sorted(open_findings),
    }
    if any(contract.get(key) != value for key, value in expected.items()):
        raise DispatchError("delta obligation does not cover the exact primary")
    validate_dispositions(delta_result, contract)
    from ..kernel.authority_projection import generations

    generation = generations(records).get(delta.get("review_generation_id"))
    primary_generation = generations(records).get(primary.get("review_generation_id"))
    if (
        generation is None
        or primary_generation is None
        or generation.lineage_id != primary_generation.lineage_id
        or generation.tree != delta.get("snapshot_tree_sha")
    ):
        raise DispatchError("delta does not belong to the primary review lineage")
    if any(row["outcome"] == "repaired" for row in delta_result["dispositions"]):
        if delta["snapshot_tree_sha"] == primary["snapshot_tree_sha"]:
            raise DispatchError("repaired findings require changed source")
        if not contract.get("acceptance_commands"):
            raise DispatchError("repaired findings require affected validation")
    for row in delta_result["dispositions"]:
        if row["outcome"] in {"repaired", "rejected"}:
            open_findings.pop(row["finding_id"])
    open_findings.update(material_findings(delta["snapshot_tree_sha"], delta_result))
    return open_findings


def projection_body(
    repo: Path,
    records: Sequence[dict[str, object]],
    task_id: str,
    *,
    pr_identity: Mapping[str, object],
) -> str:
    terminal, result = authenticated_result(
        repo, records, task_id, pr_identity=pr_identity
    )
    body = {
        "reviewed_head": pr_identity["head"],
        "result_sha256": terminal["result_sha256"],
        "findings": result["findings"],
        "dispositions": result.get("dispositions", []),
    }
    return (
        f"<!-- loopzero-review:{terminal['result_sha256']} -->\n"
        "Authenticated review result (discussion projection; thread resolution does not change authority).\n\n"
        + "```json\n"
        + json.dumps(body, sort_keys=True, indent=2)
        + "\n```\n"
    )


def require_projection(runner, *, pr_identity: Mapping[str, object], body: str) -> None:
    """Read every page and require exact published content; edited text is not proof."""
    repository, pr = pr_identity["repository"], pr_identity["pr"]
    pages = runner.json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            f"repos/{repository}/pulls/{pr}/reviews",
        ]
    )
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise DispatchError("PR review projection unavailable")
    matches = [
        row
        for page in pages
        for row in page
        if isinstance(row, dict)
        and row.get("body") == body
        and row.get("commit_id") == pr_identity["head"]
        and row.get("state") in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
    ]
    if not matches:
        raise DispatchError("authenticated result has not been projected to the PR")


def prepare_review_task(
    repo: Path,
    records: Sequence[dict[str, object]],
    task: Mapping[str, object],
    *,
    runner,
    worktree: Path,
    source: Mapping[str, object],
    requested: str,
    reviewed_tree: str | None = None,
) -> dict[str, object]:
    """Host admission seam: derive policy and obligations before registration."""
    from ..kernel.run_identity import active_run, uses_pr_review
    from ..kernel.run_log import load_entries, settings

    run_id = task.get("run_id")
    entries = load_entries(repo / settings.audit_root / "skill-runs")
    if not isinstance(run_id, str) or not any(
        row.get("run_id") == run_id for row in entries
    ):
        if "pr_identity" in task or "primary_result_sha256" in task:
            raise DispatchError("PR review requires an existing immutable run")
        return dict(task)
    if not uses_pr_review(entries, run_id):
        return dict(task)
    if task.get("review_intent") != "delivery-code-review":
        return dict(task)
    identity = task.get("pr_identity")
    if not isinstance(identity, dict) or runner is None:
        raise DispatchError("v2 formal review requires host-verified PR identity")
    if (
        identity.get("run_id") != run_id
        or identity.get("head") != source.get("head")
        or source.get("ref") != f"refs/heads/{identity.get('branch')}"
    ):
        raise DispatchError("PR review task/source owner mismatch")
    active = active_run(entries, git_branch=str(identity.get("branch")))
    if active is None or active[0] != run_id:
        raise DispatchError("PR review run does not own the active branch")
    _require_committed_source(worktree, source, reviewed_tree=reviewed_tree)
    observed = verify_pr_identity(runner, **identity)
    scoped = {**task, "pr_identity": observed}
    if requested == "delta":
        predecessor = task.get("primary_review_task_id")
        if not isinstance(predecessor, str):
            raise DispatchError("v2 delta requires authenticated primary task identity")
        from .authority import accepted_review_terminals

        terminal = accepted_review_terminals(records).get(predecessor)
        if terminal is None:
            raise DispatchError("delta primary review unavailable")
        previous_identity = terminal.get("task_contract", {}).get("pr_identity")
        if not isinstance(previous_identity, dict):
            raise DispatchError("delta primary PR registration unavailable")
        for key in ("repository", "pr", "run_id", "branch", "base", "base_sha"):
            if previous_identity.get(key) != observed.get(key):
                raise DispatchError("delta primary belongs to another PR/base")
        _require_consumed_role(records, terminal, "primary")
        derived = delta_obligations(
            repo, records, predecessor, pr_identity=previous_identity
        )
        if any(key in task and task[key] != value for key, value in derived.items()):
            raise DispatchError(
                "caller delta obligations differ from authenticated primary"
            )
        scoped.update(derived)
    elif {
        "primary_review_task_id",
        "primary_result_sha256",
        "required_finding_ids",
    } & task.keys():
        raise DispatchError("primary review cannot carry delta obligations")
    return scoped


def ensure_projection(
    repo: Path,
    records: Sequence[dict[str, object]],
    task_id: str,
    *,
    runner,
    pr_identity: Mapping[str, object],
) -> None:
    """Idempotently publish exact authenticated content, reconciling failed POSTs."""
    body = projection_body(repo, records, task_id, pr_identity=pr_identity)
    try:
        require_projection(runner, pr_identity=pr_identity, body=body)
        return
    except DispatchError as exc:
        if str(exc) != "authenticated result has not been projected to the PR":
            raise
    try:
        runner.json(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{pr_identity['repository']}/pulls/{pr_identity['pr']}/reviews",
                "--raw-field",
                f"body={body}",
                "--raw-field",
                "event=COMMENT",
                "--raw-field",
                f"commit_id={pr_identity['head']}",
            ]
        )
    except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
        # Unknown remote result must be observed before another effect is tried.
        require_projection(runner, pr_identity=pr_identity, body=body)
        return
    require_projection(runner, pr_identity=pr_identity, body=body)


def require_review_readiness(
    repo: Path,
    records: Sequence[dict[str, object]],
    *,
    runner,
    worktree: Path,
    primary_task_id: str,
    current_identity: Mapping[str, object],
    delta_task_id: str | None = None,
) -> None:
    """Review readiness only; consumer still owns required checks and merge."""
    from .authority import accepted_review_terminals

    verify_pr_identity(runner, **current_identity)
    current_task = delta_task_id or primary_task_id
    current, _ = authenticated_result(
        repo, records, current_task, pr_identity=current_identity
    )
    _require_committed_source(
        worktree, current["source_identity"], reviewed_tree=current["snapshot_tree_sha"]
    )
    from ..kernel.authority_projection import slot_state

    state = slot_state(records, str(current.get("review_generation_id")), "delivery")
    if not (state.delta_consumed if delta_task_id else state.primary_consumed):
        raise DispatchError("review has no authenticated consumed slot")
    blockers = blocking_findings(
        repo,
        records,
        primary_task_id=primary_task_id,
        current_identity=current_identity,
        delta_task_id=delta_task_id,
    )
    if blockers:
        raise DispatchError(
            "unresolved material PR review findings: " + ", ".join(sorted(blockers))
        )
    accepted = accepted_review_terminals(records)
    for task_id in dict.fromkeys((primary_task_id, current_task)):
        identity = accepted[task_id]["task_contract"]["pr_identity"]
        require_projection(
            runner,
            pr_identity=identity,
            body=projection_body(repo, records, task_id, pr_identity=identity),
        )
