"""Freeze-readiness checks with consumer-owned evidence and risk policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol


class ReviewPreflightError(RuntimeError):
    """The candidate is not ready to freeze for terminal review."""


class Runner(Protocol):
    repo: Path

    def run(self, args: list[str], *, check: bool = True): ...


EvidenceEvaluator = Callable[[Sequence[str], str], object]
RiskClassifier = Callable[[Path, str, str], Mapping[str, object]]
StrictPathClassifier = Callable[[Sequence[str]], bool]


def _nul_paths(output: str) -> tuple[str, ...]:
    paths: list[str] = []
    for path in output.split("\0"):
        if not path:
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in path):
            raise ReviewPreflightError(
                "changed path contains a control character and cannot be classified safely"
            )
        paths.append(path)
    return tuple(paths)


def collect_changed_paths(runner: Runner, base_ref: str) -> tuple[str, ...]:
    """Include committed, staged, unstaged, and untracked candidate paths."""
    base_sha = runner.run(["git", "rev-parse", base_ref]).stdout.strip()
    paths: set[str] = set()
    for command in (
        ["git", "diff", "--name-only", "-z", f"{base_sha}...HEAD"],
        ["git", "diff", "--name-only", "-z"],
        ["git", "diff", "--cached", "--name-only", "-z"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    ):
        paths.update(_nul_paths(runner.run(command).stdout))
    return tuple(sorted(paths))


def ensure_local_base_freshness(
    runner: Runner,
    base_ref: str,
    paths: Sequence[str],
    *,
    requires_strict_base: StrictPathClassifier,
) -> str:
    """Apply the injected freshness classification to the local HEAD."""
    contains = runner.run(
        ["git", "merge-base", "--is-ancestor", base_ref, "HEAD"], check=False
    )
    if contains.returncode == 0:
        return "contains-base"
    if requires_strict_base(paths):
        raise ReviewPreflightError(
            f"candidate is behind {base_ref} and touches strict-base paths"
        )
    merge_base = runner.run(["git", "merge-base", base_ref, "HEAD"]).stdout.strip()
    base_paths = set(
        _nul_paths(
            runner.run(
                ["git", "diff", "--name-only", "-z", f"{merge_base}..{base_ref}"]
            ).stdout
        )
    )
    overlap = sorted(set(paths) & base_paths)
    if overlap:
        raise ReviewPreflightError(
            f"candidate is behind {base_ref} and overlaps base changes: "
            + ", ".join(overlap[:10])
        )
    return "behind-without-overlap"


def run_preflight(
    runner: Runner,
    *,
    body: str,
    evidence_evaluator: EvidenceEvaluator,
    risk_classifier: RiskClassifier,
    requires_strict_base: StrictPathClassifier,
    base: str = "main",
) -> dict[str, object]:
    """Return immutable review inputs after all injectable policy evaluates."""
    runner.run(["git", "fetch", "--quiet", "origin", base])
    dirty = runner.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    ).stdout
    if dirty:
        raise ReviewPreflightError("terminal review requires a clean normalized candidate")
    base_ref = f"origin/{base}"
    paths = collect_changed_paths(runner, base_ref)
    visual = evidence_evaluator(paths, body)
    if not hasattr(visual, "ok") or not hasattr(visual, "reason"):
        raise ReviewPreflightError("evidence evaluator returned an invalid result")
    freshness = ensure_local_base_freshness(
        runner, base_ref, paths, requires_strict_base=requires_strict_base
    )
    merge_base = runner.run(["git", "merge-base", base_ref, "HEAD"]).stdout.strip()
    risk = dict(risk_classifier(getattr(runner, "repo", Path.cwd()), merge_base, "HEAD"))
    required = risk.get("required_sections")
    security_paths = risk.get("security_trigger_paths")
    if not isinstance(required, list) or not isinstance(security_paths, list):
        raise ReviewPreflightError("risk classifier returned an invalid result")
    return {
        "base": base_ref,
        "changed_path_count": len(paths),
        "freshness": freshness,
        "required_sections": required,
        "security_trigger_paths": security_paths,
        "visual_evidence": visual.reason,
        "visual_evidence_ok": visual.ok,
        "delivery_review_risk": risk,
    }


__all__ = [
    "EvidenceEvaluator", "ReviewPreflightError", "RiskClassifier", "Runner",
    "StrictPathClassifier", "collect_changed_paths", "ensure_local_base_freshness",
    "run_preflight",
]
