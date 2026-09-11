"""Configured PR-body validation and idempotent GitHub publication."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..config import Profile
from ..integrations.github import GitHub, GitHubError


class PublicationError(RuntimeError):
    pass


class CommandRunner(Protocol):
    def run(self, args: list[str], *, check: bool = True): ...


EVIDENCE_START = "<!-- loop-zero-evidence:start -->"
EVIDENCE_END = "<!-- loop-zero-evidence:end -->"
_ISSUE_REFERENCE = re.compile(r"(?im)^\s*(?:closes|fixes|refs|reverts)\s+(?:[^\n]*\s)?#\d+\b")
_STANDALONE_REASON = re.compile(r"(?im)^\s*Standalone-Reason\s*:\s*\S.*$")
_HEADING = re.compile(r"(?m)^##\s+(?P<name>[^\n]+?)\s*$")


@dataclass(frozen=True, slots=True)
class BodyContract:
    required_sections: tuple[str, ...]
    standalone_label_key: str = "standalone"

    @classmethod
    def from_profile(cls, profile: Profile) -> "BodyContract":
        return cls(profile.github.body_required_sections)


@dataclass(frozen=True)
class PublicationRequest:
    title: str
    body: str
    head: str
    base: str
    labels: tuple[str, ...] = ()
    expected_head: str = ""
    review_task_id: str = ""
    run_id: str = ""
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


def _section_body(body: str, name: str) -> str | None:
    matches = list(_HEADING.finditer(body))
    for index, match in enumerate(matches):
        if match.group("name").strip().casefold() == name.casefold():
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            return body[match.end():end]
    return None


def has_standalone_reason(body: str) -> bool:
    return _STANDALONE_REASON.search(body) is not None


def validate_required_sections(body: str, required_sections: Sequence[str]) -> str | None:
    for section in required_sections:
        content = _section_body(body, section)
        if content is None:
            return f"Missing required '## {section}' section."
        visible = re.sub(r"<!--.*?-->|```.*?```", "", content, flags=re.DOTALL)
        if not any(character.isalnum() for character in visible):
            return f"Required '## {section}' section must contain a result."
    return None


def validate_contract(
    body: str,
    *,
    contract: BodyContract,
    draft: bool = False,
    standalone: bool = False,
) -> str | None:
    if body.count("<!--") != body.count("-->"):
        return "PR body contains an unclosed HTML comment."
    if error := validate_required_sections(body, contract.required_sections):
        return error
    if draft or standalone or has_standalone_reason(body) or _ISSUE_REFERENCE.search(body):
        return None
    return "No work reference found and no standalone reason was supplied."


def require_local_publication_prerequisites(
    *, head: str, expected_head: str, runner: CommandRunner
) -> None:
    """Reject detached, dirty, moved, or differently named publication sources."""
    if re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise PublicationError("expected head must be exactly 40 lowercase hex")
    local_head = runner.run(["git", "rev-parse", "HEAD"]).stdout.strip()
    symbolic = runner.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], check=False
    )
    if symbolic.returncode != 0:
        raise PublicationError("publication requires a named branch")
    if local_head != expected_head:
        raise PublicationError("local HEAD does not match expected head")
    if symbolic.stdout.strip() != head:
        raise PublicationError("checked-out branch does not match publication head")
    if runner.run(["git", "status", "--porcelain"]).stdout.strip():
        raise PublicationError("publication requires a clean worktree")


def changed_paths_between(
    runner: CommandRunner, base_ref: str, head_ref: str
) -> tuple[str, ...]:
    """Return both sides of renames by disabling rename detection."""
    output = runner.run(
        ["git", "diff", "--name-only", "-z", "--no-renames", f"{base_ref}..{head_ref}"]
    ).stdout
    paths = tuple(path for path in output.split("\0") if path)
    if any(any(ord(character) < 32 for character in path) for path in paths):
        raise PublicationError("changed path contains a control character")
    return paths


def require_obligation_acknowledgment(
    *, base: str, head: str, body: str,
    evaluator,
) -> None:
    """Run the consumer's obligation evaluator without importing its policy."""
    try:
        missing = evaluator(base, head, acknowledgment_text=body)
    except Exception as exc:
        raise PublicationError(f"cannot evaluate obligation changes: {exc}") from exc
    if missing:
        raise PublicationError("changed obligations require a substantive acknowledgment")


def require_resolved_review_threads(github: GitHub, *, pr: int) -> None:
    """Read paginated GitHub threads through the sole integration boundary."""
    repo = github.repository()
    cursor: str | None = None
    unresolved: list[dict[str, object]] = []
    query = (
        "query($owner:String!,$name:String!,$pr:Int!,$after:String){repository(owner:$owner,name:$name){"
        "pullRequest(number:$pr){reviewThreads(first:100,after:$after){nodes{id isResolved path comments(first:1){nodes{url}}}"
        "pageInfo{hasNextPage endCursor}}}}}"
    )
    while True:
        payload = github.api(
            "graphql", fields={"query": query, "variables": {
                "owner": repo.owner, "name": repo.name, "pr": pr, "after": cursor,
            }}
        )
        try:
            page = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = page["nodes"]
            info = page["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise PublicationError("GitHub returned malformed review-thread evidence") from exc
        if not isinstance(nodes, list) or not isinstance(info, Mapping):
            raise PublicationError("GitHub returned malformed review-thread evidence")
        unresolved.extend(
            thread for thread in nodes
            if isinstance(thread, dict) and thread.get("isResolved") is False
        )
        if info.get("hasNextPage") is not True:
            break
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise PublicationError("GitHub review-thread pagination is malformed")
    if unresolved:
        raise PublicationError(
            f"PR has {len(unresolved)} unresolved review thread(s), including outdated threads"
        )


def trusted_publication_base_head(github: GitHub, *, pr: int) -> tuple[str, str]:
    """Return the remote base/head SHAs for one repository-bound PR."""
    repo = github.repository()
    payload = github.api(f"repos/{repo.slug}/pulls/{pr}")
    try:
        base_sha = payload["base"]["sha"]
        head_sha = payload["head"]["sha"]
    except (KeyError, TypeError) as exc:
        raise PublicationError("GitHub returned malformed PR head identity") from exc
    if any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (base_sha, head_sha)):
        raise PublicationError("GitHub returned invalid PR commit identity")
    return base_sha, head_sha


def _loopzero_evidence(
    *, artifact: Mapping[str, object], review_task_id: str | None, run_id: str
) -> str:
    payload = {
        "contract": "loop-zero-v1",
        "run_id": run_id,
        "base_sha": artifact["base_sha"],
        "head_sha": artifact["head_sha"],
        "head_tree_sha": artifact["head_tree_sha"],
        "review_task_id": review_task_id or None,
    }
    return f"{EVIDENCE_START}\n```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```\n{EVIDENCE_END}"


def bind_loopzero_evidence(
    body: str,
    *,
    artifact: Mapping[str, object],
    review_task_id: str | None,
    run_id: str,
    validation_section: str = "Validation",
) -> str:
    block = _loopzero_evidence(
        artifact=artifact, review_task_id=review_task_id, run_id=run_id
    )
    if body.count(EVIDENCE_START) != body.count(EVIDENCE_END) or body.count(EVIDENCE_START) > 1:
        raise PublicationError("loop-zero evidence block is malformed or duplicated")
    if EVIDENCE_START in body:
        start = body.index(EVIDENCE_START)
        end = body.index(EVIDENCE_END, start) + len(EVIDENCE_END)
        return body[:start] + block + body[end:]
    section = re.search(rf"(?m)^##\s+{re.escape(validation_section)}\s*$", body)
    if section is None:
        raise PublicationError(f"loop-zero evidence requires the {validation_section} section")
    return body[: section.end()] + "\n\n" + block + body[section.end():]


def validate_loopzero_evidence(
    body: str, *, artifact: Mapping[str, object], review_task_id: str | None, run_id: str
) -> None:
    if body.count(EVIDENCE_START) != 1 or body.count(EVIDENCE_END) != 1:
        raise PublicationError("loop-zero requires exactly one PR evidence block")
    if bind_loopzero_evidence(
        body, artifact=artifact, review_task_id=review_task_id, run_id=run_id
    ) != body:
        raise PublicationError("loop-zero PR evidence does not match the admitted source")


_PUBLISHED_AUTHORITY_FIELDS = (
    "admission_tier_floor", "base", "expected_head", "head", "pr",
    "review_exemption", "review_risk_json", "review_risk_sha256",
    "review_risk_tier", "review_task_id", "run_id",
)


def write_evidence(evidence_dir: Path, record: dict[str, object]) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    fd, temporary = tempfile.mkstemp(prefix=".publication-", dir=evidence_dir)
    try:
        previous = path.read_bytes() if path.exists() else b""
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
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


def write_published_evidence_once(
    evidence_dir: Path, record: dict[str, object], *, generation: int
) -> Path:
    if generation:
        record = {**record, "review_reentry_generation": generation}
    matches: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(evidence_dir.glob("*.jsonl")) if evidence_dir.is_dir() else ():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicationError(f"{path}:{number}: invalid publication evidence") from exc
            if not isinstance(candidate, dict):
                raise PublicationError(f"{path}:{number}: publication evidence is not an object")
            if all(candidate.get(field) == record.get(field) for field in _PUBLISHED_AUTHORITY_FIELDS) and candidate.get("review_reentry_generation", 0) == generation:
                matches.append((path, candidate))
    if len(matches) > 1:
        raise PublicationError("publication evidence authority is duplicated")
    if matches:
        path, candidate = matches[0]
        if candidate != record:
            # Adoption metadata is deliberately not allowed to remint authority.
            return path
        return path
    return write_evidence(evidence_dir, record)


def publish(
    github: GitHub,
    request: PublicationRequest,
    *,
    contract: BodyContract,
    evidence_dir: Path,
) -> PublicationResult:
    labels = github.labels(request.labels)
    standalone = contract.standalone_label_key in request.labels
    if error := validate_contract(request.body, contract=contract, standalone=standalone):
        raise PublicationError(error)
    repo = github.repository()
    try:
        payload = github.api(
            f"repos/{repo.slug}/pulls",
            method="POST",
            fields={"title": request.title, "body": request.body, "head": request.head, "base": request.base},
        )
        if not isinstance(payload, dict) or type(payload.get("number")) is not int or not isinstance(payload.get("html_url"), str):
            raise PublicationError("GitHub returned malformed PR identity")
        mutations = 0
        if labels:
            github.api(
                f"repos/{repo.slug}/issues/{payload['number']}/labels",
                method="POST", fields={"labels": list(labels)},
            )
            mutations = 1
    except GitHubError as exc:
        raise PublicationError(str(exc)) from exc
    record = {
        "status": "published", "pr": payload["number"], "url": payload["html_url"],
        "base": request.base, "head": request.head,
        "expected_head": request.expected_head or request.head,
        "review_task_id": request.review_task_id or None, "run_id": request.run_id,
        "review_risk_json": request.review_risk_json,
        "review_risk_sha256": request.review_risk_sha256,
        "review_risk_tier": request.review_risk_tier,
        "admission_tier_floor": request.admission_tier_floor,
        "review_exemption": "T0" if request.admission_tier_floor == "T0" else None,
    }
    evidence = write_published_evidence_once(
        evidence_dir, record, generation=request.review_reentry_generation
    )
    return PublicationResult(payload["number"], payload["html_url"], evidence, mutations)


__all__ = [
    "BodyContract", "PublicationError", "PublicationRequest", "PublicationResult",
    "bind_loopzero_evidence", "changed_paths_between", "has_standalone_reason", "publish",
    "require_local_publication_prerequisites", "require_obligation_acknowledgment",
    "require_resolved_review_threads", "trusted_publication_base_head",
    "validate_contract", "validate_loopzero_evidence", "validate_required_sections",
    "write_evidence", "write_published_evidence_once",
]
