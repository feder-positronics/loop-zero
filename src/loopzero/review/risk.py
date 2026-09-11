"""Canonical candidate-bound review-risk artifact and publication verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Profile
from ._ci_path_classifier import classify_paths


@dataclass(frozen=True)
class RiskSettings:
    path_classes: Mapping[str, Sequence[str]]
    security_patterns: tuple[str, ...]
    required_sections: tuple[str, ...]
    path_class_parents: Mapping[str, str]


_SETTINGS: ContextVar[RiskSettings | None] = ContextVar(
    "review_risk_settings", default=None
)


def configure(profile: Profile) -> None:
    """Install validated consumer path tables and review-section policy."""
    from . import _ci_path_classifier, _security_scope

    configured = RiskSettings(
        path_classes=profile.path_classes,
        security_patterns=tuple(profile.security_patterns),
        required_sections=tuple(profile.required_sections),
        path_class_parents=getattr(profile, "path_class_parents", {}),
    )
    _SETTINGS.set(configured)
    _ci_path_classifier.configure(
        configured.path_classes, configured.path_class_parents
    )
    _security_scope.configure(
        security_patterns=configured.security_patterns,
        required_sections=configured.required_sections,
    )
from ..kernel.gitscope import trusted_git_command
from ..kernel.sandbox import environment as sandbox_environment
from ._ci_path_classifier import classify_paths

SCHEMA_VERSION = "delivery-review-risk-v1"
TIER_RANK = {"T0": 0, "T1": 1, "T2": 2}
_OID_RE = re.compile(r"[0-9a-f]{40,64}")
_DOC_SUFFIXES = (".md", ".mdx", ".html")
_ARTIFACT_KEYS = {
    "base_sha",
    "changed_paths",
    "changed_paths_digest",
    "classifier_version",
    "diff_sha256",
    "head_sha",
    "head_tree_sha",
    "reasons",
    "required_sections",
    "schema_version",
    "security_trigger_paths",
    "tier",
}
_BINDING_FIELDS = {
    "base_sha",
    "changed_paths",
    "changed_paths_digest",
    "diff_sha256",
    "head_sha",
    "head_tree_sha",
    "schema_version",
}


class ReviewRiskError(RuntimeError):
    """The exact candidate cannot carry the claimed review-risk authority."""


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
        preserve_output_bytes: bool = False,
    ) -> CommandOutcome: ...


def _runner(
    repo: Path,
    *,
    origin_url: str | None = None,
    git_config_sha256: str | None = None,
) -> CommandRunner:
    class GitRunner:
        def __init__(self) -> None:
            self.origin_url = origin_url
            self.git_config_sha256 = git_config_sha256

        def run(
            self,
            args: list[str],
            *,
            check: bool = True,
            preserve_output_bytes: bool = False,
        ):
            command = args
            if args and args[0] == "git":
                command = trusted_git_command(repo, *args[1:])
            completed = subprocess.run(
                command,
                cwd=repo,
                env=sandbox_environment(os.environ),
                capture_output=True,
                text=not preserve_output_bytes,
                check=False,
            )
            if check and completed.returncode:
                raise ReviewRiskError(
                    (completed.stderr or completed.stdout or b"git failed").decode(
                        errors="replace"
                    )
                    if preserve_output_bytes
                    else str(completed.stderr or completed.stdout or "git failed")
                )
            return completed

    return GitRunner()


def _run(
    git: CommandRunner,
    args: list[str],
    *,
    check: bool = True,
    preserve_output_bytes: bool = False,
) -> CommandOutcome:
    try:
        return git.run(
            args,
            check=check,
            preserve_output_bytes=preserve_output_bytes,
        )
    except Exception as exc:
        raise ReviewRiskError(f"Git evidence collection failed: {exc}") from exc


def _resolve_commit(git: CommandRunner, ref: str) -> str:
    oid = _run(
        git, ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"]
    ).stdout.strip()
    if _OID_RE.fullmatch(oid) is None:
        raise ReviewRiskError(f"{ref!r} did not resolve to a commit")
    return oid


def _nul_paths(output: str) -> tuple[str, ...]:
    paths = tuple(path for path in output.split("\0") if path)
    if any(
        any(ord(character) < 32 or ord(character) == 127 for character in path)
        for path in paths
    ):
        raise ReviewRiskError("changed path contains a control character")
    return tuple(sorted(set(paths)))


def _tree_modes(
    git: CommandRunner, refs: Sequence[str], paths: Sequence[str]
) -> tuple[tuple[str, str, str], ...]:
    observed: set[tuple[str, str, str]] = set()
    for ref in refs:
        if not paths:
            continue
        listing = _run(
            git,
            ["git", "ls-tree", "-rz", "--full-tree", ref, "--", *paths],
        ).stdout
        for record in listing.split("\0"):
            if not record:
                continue
            metadata, separator, path = record.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise ReviewRiskError("Git tree returned malformed object metadata")
            mode, object_type, _oid = fields
            if path in paths:
                observed.add((path, mode, object_type))
    return tuple(sorted(observed))


def _classifier_version() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    digest.update(SCHEMA_VERSION.encode())
    for name in (
        "_ci_path_classifier.py",
        "risk.py",
        "_security_scope.py",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def canonical_review_risk_json(artifact: Mapping[str, object]) -> str:
    return json.dumps(
        dict(artifact),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_review_risk(payload: str) -> dict[str, object]:
    duplicates: list[str] = []

    def object_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    try:
        parsed = json.loads(payload, object_pairs_hook=object_hook)
    except json.JSONDecodeError as exc:
        raise ReviewRiskError("review-risk artifact is not valid JSON") from exc
    if duplicates:
        raise ReviewRiskError(
            "review-risk artifact contains duplicate keys: "
            + ", ".join(sorted(set(duplicates)))
        )
    if not isinstance(parsed, dict):
        raise ReviewRiskError("review-risk artifact must be a JSON object")
    if canonical_review_risk_json(parsed) != payload:
        raise ReviewRiskError("review-risk artifact bytes are not canonical")
    return parsed


def _artifact(
    repo: Path,
    base_ref: str,
    head_ref: str,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    from ._security_scope import (
        SecurityReviewScopeError,
        required_review_sections,
        security_trigger_paths_between,
    )

    git = runner or _runner(repo)
    base_sha = _resolve_commit(git, base_ref)
    head_sha = _resolve_commit(git, head_ref)
    head_tree_sha = _run(
        git, ["git", "rev-parse", f"{head_sha}^{{tree}}"]
    ).stdout.strip()
    if _OID_RE.fullmatch(head_tree_sha) is None:
        raise ReviewRiskError("candidate tree identity is invalid")
    changed_paths = _nul_paths(
        _run(
            git,
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                base_sha,
                head_sha,
                "--",
            ],
        ).stdout
    )
    if not changed_paths:
        raise ReviewRiskError("review-risk artifact requires a non-empty candidate")
    diff = _run(
        git,
        [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            base_sha,
            head_sha,
            "--",
        ],
        preserve_output_bytes=True,
    ).stdout
    modes = _tree_modes(git, (base_sha, head_sha), changed_paths)
    special_mode = any(mode != "100644" or kind != "blob" for _, mode, kind in modes)
    path_classes = classify_paths(changed_paths)
    try:
        security_paths = security_trigger_paths_between(
            repo, base_sha, head_sha, runner=git
        )
    except SecurityReviewScopeError as exc:
        raise ReviewRiskError(f"security classification failed: {exc}") from exc

    conventional_docs = all(
        path.startswith("docs/") and path.endswith(_DOC_SUFFIXES)
        for path in changed_paths
    )
    complete_modes = {path for path, _, _ in modes} == set(changed_paths)
    classifier_disagreement = conventional_docs and not path_classes["docs"]
    if security_paths:
        tier = "T2"
        reasons = ["security-review-required"]
    elif (
        conventional_docs
        and complete_modes
        and not special_mode
        and not classifier_disagreement
    ):
        tier = "T0"
        reasons = ["docs-only"]
    else:
        tier = "T1"
        reasons = ["review-required"]
        if special_mode or not complete_modes:
            reasons.append("special-git-mode")
        if classifier_disagreement:
            reasons.append("classifier-disagreement")
        if not any(path_classes.values()):
            reasons.append("unknown-path")

    sections = [] if tier == "T0" else list(required_review_sections(security_paths))
    return {
        "base_sha": base_sha,
        "changed_paths": list(changed_paths),
        "changed_paths_digest": hashlib.sha256(
            "\0".join(changed_paths).encode("utf-8") + b"\0"
        ).hexdigest(),
        "classifier_version": _classifier_version(),
        "diff_sha256": hashlib.sha256(os.fsencode(diff)).hexdigest(),
        "head_sha": head_sha,
        "head_tree_sha": head_tree_sha,
        "reasons": reasons,
        "required_sections": sections,
        "schema_version": SCHEMA_VERSION,
        "security_trigger_paths": list(security_paths),
        "tier": tier,
    }


def compute_review_risk(
    repo: Path,
    base_ref: str,
    head_ref: str,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, str]:
    artifact = _artifact(repo, base_ref, head_ref, runner=runner)
    payload = canonical_review_risk_json(artifact)
    return {
        "review_risk_json": payload,
        "review_risk_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def verify_review_risk(
    repo: Path,
    payload: str,
    digest: str,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    artifact = parse_review_risk(payload)
    actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != actual_digest:
        raise ReviewRiskError(
            "review-risk artifact digest does not match canonical bytes"
        )
    artifact_base = artifact.get("base_sha")
    artifact_head = artifact.get("head_sha")
    if (
        not isinstance(artifact_base, str)
        or _OID_RE.fullmatch(artifact_base) is None
        or not isinstance(artifact_head, str)
        or _OID_RE.fullmatch(artifact_head) is None
    ):
        raise ReviewRiskError("review-risk artifact requires commit object IDs")
    expected = _artifact(
        repo,
        artifact_base,
        artifact_head,
        runner=runner,
    )
    if set(artifact) != _ARTIFACT_KEYS:
        raise ReviewRiskError("review-risk artifact has an invalid field set")
    tier = artifact.get("tier")
    reasons = artifact.get("reasons")
    sections = artifact.get("required_sections")
    changed_paths = artifact.get("changed_paths")
    security_paths = artifact.get("security_trigger_paths")
    if tier not in TIER_RANK:
        raise ReviewRiskError("review-risk artifact tier is invalid")
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(reason, str) and reason for reason in reasons)
    ):
        raise ReviewRiskError("review-risk artifact reasons are invalid")
    if not isinstance(changed_paths, list) or not all(
        isinstance(path, str) and path for path in changed_paths
    ):
        raise ReviewRiskError("review-risk artifact changed paths are invalid")
    if not isinstance(security_paths, list) or not all(
        isinstance(path, str) and path for path in security_paths
    ):
        raise ReviewRiskError("review-risk artifact security paths are invalid")
    if security_paths != sorted(set(security_paths)):
        raise ReviewRiskError("review-risk artifact security paths are not canonical")
    if not set(security_paths).issubset(set(changed_paths)):
        raise ReviewRiskError("review-risk artifact security paths escape its diff")
    expected_sections = (
        [] if tier == "T0" else ["code", "security"] if security_paths else ["code"]
    )
    expected_reason = (
        "docs-only"
        if tier == "T0"
        else "security-review-required"
        if security_paths
        else "review-required"
    )
    if sections != expected_sections or reasons[0] != expected_reason:
        raise ReviewRiskError("review-risk artifact classification is inconsistent")
    if (tier == "T2") != bool(security_paths):
        raise ReviewRiskError("review-risk artifact security tier is inconsistent")
    if any(artifact.get(field) != expected.get(field) for field in _BINDING_FIELDS):
        raise ReviewRiskError("review-risk artifact conflicts with the exact candidate")
    if artifact.get("classifier_version") == expected.get("classifier_version"):
        if artifact != expected:
            raise ReviewRiskError(
                "review-risk artifact conflicts with the current classifier"
            )
    elif (
        not isinstance(artifact.get("classifier_version"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("classifier_version")))
        is None
    ):
        raise ReviewRiskError("review-risk artifact classifier version is invalid")

    effective_security_paths = sorted(
        set(security_paths) | set(expected["security_trigger_paths"])
    )
    effective_tier = strictest_tier(str(tier), str(expected["tier"]))
    effective_sections = (
        []
        if effective_tier == "T0"
        else ["code", "security"]
        if effective_security_paths
        else ["code"]
    )
    return {
        "artifact": artifact,
        "current_artifact": expected,
        "effective_required_sections": effective_sections,
        "effective_security_trigger_paths": effective_security_paths,
        "effective_tier": effective_tier,
    }


def strictest_tier(*tiers: str) -> str:
    if not tiers or any(tier not in TIER_RANK for tier in tiers):
        raise ReviewRiskError("review-risk tier is invalid")
    return max(tiers, key=TIER_RANK.__getitem__)


def load_publication_review_risk(
    evidence_dir: Path,
    *,
    pr: int,
    run_id: str,
    expected_head: str,
    generation: int,
) -> dict[str, object]:
    """Load exactly one canonical publication receipt for one frozen candidate.

    ``generation`` is the run's authenticated review-reentry count; callers
    derive it from authority records, never from the evidence directory path.
    """
    if pr <= 0:
        raise ReviewRiskError("publication review-risk lookup requires a positive PR")
    if re.fullmatch(r"sr_[0-9a-f]{32}", run_id) is None:
        raise ReviewRiskError("publication review-risk lookup requires a valid run ID")
    if _OID_RE.fullmatch(expected_head) is None:
        raise ReviewRiskError(
            "publication review-risk lookup requires an exact head commit"
        )

    matches: list[dict[str, object]] = []
    for path in sorted(evidence_dir.glob("*.jsonl")) if evidence_dir.is_dir() else ():
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            duplicates: list[str] = []

            def object_hook(
                pairs: list[tuple[str, object]],
                duplicate_keys: list[str] = duplicates,
            ) -> dict[str, object]:
                record: dict[str, object] = {}
                for key, value in pairs:
                    if key in record:
                        duplicate_keys.append(key)
                    record[key] = value
                return record

            try:
                record = json.loads(line, object_pairs_hook=object_hook)
            except json.JSONDecodeError as exc:
                raise ReviewRiskError(
                    f"{path}:{line_number}: invalid publication evidence"
                ) from exc
            if duplicates:
                raise ReviewRiskError(
                    f"{path}:{line_number}: duplicate publication evidence keys"
                )
            if not isinstance(record, dict):
                raise ReviewRiskError(
                    f"{path}:{line_number}: publication evidence is not an object"
                )
            if (
                record.get("review_reentry_generation", 0) == generation
                and record.get("status") == "published"
                and record.get("pr") == pr
                and record.get("run_id") == run_id
                and record.get("expected_head") == expected_head
            ):
                matches.append(record)

    if not matches:
        raise ReviewRiskError(
            "published review-risk evidence is missing for the PR/run/exact head"
        )
    if len(matches) != 1:
        raise ReviewRiskError(
            "published review-risk evidence is duplicated for the PR/run/exact head"
        )
    return matches[0]


def verify_publication_review_risk(
    repo: Path,
    evidence_dir: Path,
    *,
    pr: int,
    run_id: str,
    expected_head: str,
    expected_base: str | None = None,
    runner: CommandRunner | None = None,
    authority_repo: Path | None = None,
) -> dict[str, object]:
    """Verify publication evidence and rederive current exact-head risk.

    ``authority_repo`` (default ``repo``) is a caller-pinned trust root, not an
    untrusted candidate choice. Canonical callers validate it through live Git
    binding (real primary Git directory, unique worktree registration and common
    directory equality) and derive publication evidence from that same root.
    Neither evidence-path arithmetic nor mutable worktree discovery replaces it.
    """
    from ..delivery.reentry import publication_generation
    from ..kernel.authority_store import load_authority_records

    generation = publication_generation(
        load_authority_records(authority_repo or repo, 30), run_id
    )
    record = load_publication_review_risk(
        evidence_dir,
        pr=pr,
        run_id=run_id,
        expected_head=expected_head,
        generation=generation,
    )
    payload = record.get("review_risk_json")
    digest = record.get("review_risk_sha256")
    if not isinstance(payload, str) or not isinstance(digest, str):
        raise ReviewRiskError("published review-risk bytes or digest are missing")
    verification = verify_review_risk(repo, payload, digest, runner=runner)
    artifact = verification["artifact"]
    if artifact.get("head_sha") != expected_head:
        raise ReviewRiskError("published review-risk artifact names a different head")
    if expected_base is not None and artifact.get("base_sha") != expected_base:
        raise ReviewRiskError("published review-risk artifact names a different base")

    recorded_tier = record.get("review_risk_tier")
    admission_floor = record.get("admission_tier_floor")
    if (
        not isinstance(recorded_tier, str)
        or strictest_tier(str(artifact["tier"]), recorded_tier) != recorded_tier
    ):
        raise ReviewRiskError("published review-risk tier is weaker than its artifact")
    if not isinstance(admission_floor, str):
        raise ReviewRiskError("published review-risk admission floor is missing")
    if strictest_tier(recorded_tier, admission_floor) != admission_floor:
        raise ReviewRiskError("published review-risk admission floor became weaker")
    effective_tier = strictest_tier(
        str(verification["effective_tier"]), admission_floor
    )
    if effective_tier != admission_floor:
        raise ReviewRiskError("published review-risk admission floor became weaker")

    review_task_id = record.get("review_task_id")
    if admission_floor == "T0":
        if review_task_id is not None or record.get("review_exemption") != "T0":
            raise ReviewRiskError(
                "T0 publication evidence has invalid review authority"
            )
    elif (
        not isinstance(review_task_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", review_task_id) is None
        or record.get("review_exemption") is not None
    ):
        raise ReviewRiskError("reviewed publication evidence has invalid authority")

    return {
        "admission_tier_floor": admission_floor,
        "artifact": artifact,
        "current_artifact": verification["current_artifact"],
        "effective_tier": effective_tier,
        "review_task_id": review_task_id,
    }


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--authority-repo", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--verify-publication", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-base")
    parser.add_argument("--origin-url")
    parser.add_argument("--git-config-sha256")
    args = parser.parse_args(argv)
    try:
        if bool(args.origin_url) != bool(args.git_config_sha256):
            raise ReviewRiskError(
                "--origin-url and --git-config-sha256 must be supplied together"
            )
        if args.verify_publication:
            if (
                args.evidence_dir is None
                or args.pr is None
                or args.run_id is None
                or args.expected_head is None
                or args.expected_base is None
                or args.origin_url is None
                or args.git_config_sha256 is None
            ):
                raise ReviewRiskError(
                    "publication verification requires evidence dir, PR, run, head, "
                    "base, origin URL, and Git-config digest"
                )
            runner = _runner(
                args.repo,
                origin_url=args.origin_url,
                git_config_sha256=args.git_config_sha256,
            )
            print(
                json.dumps(
                    verify_publication_review_risk(
                        args.repo,
                        args.evidence_dir,
                        pr=args.pr,
                        run_id=args.run_id,
                        expected_head=args.expected_head,
                        expected_base=args.expected_base,
                        runner=runner,
                        authority_repo=args.authority_repo or args.repo,
                    ),
                    sort_keys=True,
                )
            )
        else:
            if args.base is None or args.head is None:
                raise ReviewRiskError("risk computation requires --base and --head")
            print(
                json.dumps(
                    compute_review_risk(args.repo, args.base, args.head), sort_keys=True
                )
            )
    except ReviewRiskError as exc:
        print(f"delivery-review-risk: failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


from ._ci_path_classifier import write_github_outputs  # noqa: E402,F401
from ._security_scope import (  # noqa: E402,F401
    DELTA_SECURITY_PATTERNS,
    SecurityReviewScopeError,
    required_review_sections,
    security_trigger_paths,
    security_trigger_paths_between,
    untracked_security_trigger_paths,
)
