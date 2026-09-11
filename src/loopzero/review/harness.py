"""Run one bounded, exact-scope review through the alternate AI harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..kernel.authority_store import (
    create_coordinator_authority,
    load_authority_records,
)
from ..config import Profile
from ..runners.contract import (
    RuntimeCapabilityProfile,
    RuntimeCommercialMode,
    RuntimeRequest,
    RuntimeStatus,
    SubscriptionEligibility,
)
from ..runners.registry import RUNTIME_REGISTRY, RuntimeRegistry
from ..runners.settings import RuntimeSettings
from . import routing
from ..kernel.gitscope import (
    ReviewSnapshot,
    primary_repo_root,
)
from ..kernel import events as kernel_events
from .authority import (
    authenticated_verdicts,
    latest_accepted_review_terminal,
)
from .evidence import (
    append_finding_records,
)
from .routing import (
    WORK_UNIT_HISTORY_DAYS,
)
from .findings import LedgerError  # noqa: E402
from .chain import (  # noqa: E402
    ReviewChainError,
    build_review_chain_advisory_receipt,
)

CLAUDE_BUDGET_USD = 1.0
CLAUDE_TIMEOUT_SECONDS = 120
CLAUDE_MAX_PROMPT_CHARS = 225_000
CODEX_REVIEW_TIMEOUT_SECONDS = 600
CODEX_DOCUMENT_TIMEOUT_SECONDS = 300
DIFF_CONTEXT_LINES = 10
LONG_DIFF_LINE_CHARS = 20_000

_PROFILE: ContextVar[Profile | None] = ContextVar("cross_harness_profile", default=None)
_AUTHORITY_APPEND: ContextVar[Callable[..., None] | None] = ContextVar(
    "cross_harness_authority_append", default=None
)


def configure(
    profile: Profile, *, append_authority: Callable[..., None] | None = None
) -> None:
    _PROFILE.set(profile)
    _AUTHORITY_APPEND.set(append_authority)


def append_authoritative_record(*args, **kwargs) -> None:
    append = _AUTHORITY_APPEND.get()
    if append is None:
        raise CrossHarnessError("authoritative advisory deposition is not configured")
    append(*args, **kwargs)


class CrossHarnessError(RuntimeError):
    """Raised when the requested review scope cannot be prepared safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ClaudeResult:
    text: str
    cost_usd: float | None
    is_error: bool


@dataclass(frozen=True)
class AdvisoryResult:
    findings: tuple[dict[str, object], ...]
    finding_count: int
    max_severity: str
    runtime: object | None = None


# Harness detection stays in consumer composition.

# Native command construction lives in runner adapters.

# Native command construction lives in runner adapters.

def _read_scoped_paths(repo: Path, paths: Sequence[str]) -> str:
    if not paths:
        raise CrossHarnessError("files/document scope requires at least one --path")
    sections: list[str] = []
    for raw_path in paths:
        candidate = (repo / raw_path).resolve()
        if not candidate.is_relative_to(repo):
            raise CrossHarnessError(f"scope path is outside the repository: {raw_path}")
        if not candidate.is_file():
            raise CrossHarnessError(f"scope path is not a file: {raw_path}")
        relative = candidate.relative_to(repo)
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CrossHarnessError(
                f"scope path is not UTF-8 text: {raw_path}"
            ) from exc
        sections.append(f"===== {relative.as_posix()} =====\n{content}")
    return "\n\n".join(sections)


def _compact_long_line_diff(path: str, diff_text: str) -> str:
    kept_lines = [
        line
        for line in diff_text.splitlines()
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@ ", "+", "-"))
    ]
    return (
        f"===== COMPACTED LONG-LINE DIFF: {path} =====\n"
        "Unchanged word context omitted; additions and deletions preserved.\n"
        + "\n".join(kept_lines)
        + "\n"
    )


def _collect_tracked_diff(runner: Any, *, diff_range: str) -> tuple[str, str]:
    paths_result = runner.run(["git", "diff", "--name-only", "-z", diff_range])
    if paths_result.returncode != 0:
        raise CrossHarnessError(
            (paths_result.stderr or "git diff --name-only failed").strip()
        )

    diff_sections: list[str] = []
    for path in (item for item in paths_result.stdout.split("\0") if item):
        diff = runner.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                f"--unified={DIFF_CONTEXT_LINES}",
                diff_range,
                "--",
                path,
            ]
        )
        if diff.returncode != 0:
            raise CrossHarnessError((diff.stderr or "git diff failed").strip())
        diff_text = diff.stdout
        if max((len(line) for line in diff_text.splitlines()), default=0) > (
            LONG_DIFF_LINE_CHARS
        ):
            word_diff = runner.run(
                [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--unified=0",
                    "--word-diff=porcelain",
                    diff_range,
                    "--",
                    path,
                ]
            )
            if word_diff.returncode != 0:
                raise CrossHarnessError(
                    (word_diff.stderr or "git word diff failed").strip()
                )
            diff_text = _compact_long_line_diff(path, word_diff.stdout)
        diff_sections.append(diff_text)
    return "".join(diff_sections), paths_result.stderr


def collect_scope(
    runner: Any,
    repo: Path,
    *,
    scope: str,
    diff_range: str,
    paths: Sequence[str],
) -> str:
    if scope in {"files", "document"}:
        normalized_paths: list[str] = []
        resolved_repo = repo.resolve()
        for raw_path in paths:
            candidate = (resolved_repo / raw_path).resolve()
            if not candidate.is_relative_to(resolved_repo):
                raise CrossHarnessError(
                    f"scope path is outside the repository: {raw_path}"
                )
            normalized_paths.append(candidate.relative_to(resolved_repo).as_posix())
        visible = runner.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                *normalized_paths,
            ]
        )
        if visible.returncode != 0:
            raise CrossHarnessError("could not validate scoped paths with git")
        visible_paths = {
            line.strip() for line in visible.stdout.splitlines() if line.strip()
        }
        hidden_paths = set(normalized_paths) - visible_paths
        if hidden_paths:
            raise CrossHarnessError(
                "scope contains ignored or unavailable path(s): "
                + ", ".join(sorted(hidden_paths))
            )
        return _read_scoped_paths(repo.resolve(), paths)
    if scope != "diff":
        raise CrossHarnessError(f"unsupported scope: {scope}")
    stat = runner.run(["git", "diff", "--stat", diff_range])
    diff_text, diff_stderr = _collect_tracked_diff(
        runner,
        diff_range=diff_range,
    )
    untracked = runner.run(["git", "ls-files", "--others", "--exclude-standard"])
    if stat.returncode != 0 or untracked.returncode != 0:
        detail = stat.stderr or diff_stderr or untracked.stderr or "git diff failed"
        raise CrossHarnessError(detail.strip())
    untracked_paths = [
        line.strip() for line in untracked.stdout.splitlines() if line.strip()
    ]
    untracked_text = (
        _read_scoped_paths(repo.resolve(), untracked_paths) if untracked_paths else ""
    )
    if not diff_text.strip() and not untracked_text:
        raise CrossHarnessError("reviewed diff is empty")
    scope_text = f"Diff stat:\n{stat.stdout}\n\nDiff:\n{diff_text}"
    if untracked_text:
        scope_text += f"\n\nUntracked files:\n{untracked_text}"
    return scope_text


def review_prompt(skill: str, scope: str, scope_text: str) -> str:
    return (
        f"You are doing an advisory {skill} second pass. Review only the exact "
        f"{scope} scope below. Report correctness, security, regression, or "
        "workflow-contract risks; do not propose unrelated improvements. Return "
        "only one JSON object with keys findings and limitations. findings is an "
        "array of at most 5 objects with exactly severity, claim, path, line_start, "
        "and line_end; severity is critical, important, or suggestion, and nullable "
        "anchors use JSON null. limitations is an array of at most 3 strings. Use "
        "an empty findings array when clean. Do not ask to run commands; you have "
        "no tools.\n\n"
        f"{scope_text}"
    )


def parse_claude_result(raw: str) -> ClaudeResult:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CrossHarnessError("Claude did not return JSON output") from exc
    if not isinstance(payload, dict):
        raise CrossHarnessError("Claude JSON output has an unexpected shape")
    text = str(payload.get("result") or "").strip()
    raw_cost = payload.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, int | float) else None
    return ClaudeResult(
        text=text,
        cost_usd=cost,
        is_error=bool(payload.get("is_error")),
    )


_FINDING_TRAILER = re.compile(
    r"(?:^|\n)[ \t]*CROSS_HARNESS_FINDINGS:\s*count=(\d+)\s+"
    r"max_severity=(critical|important|suggestion|none)[ \t]*(?:\r?\n)?\Z",
    re.IGNORECASE,
)
_NO_FINDINGS = re.compile(r"^\s*no findings[.!]?\s*$", re.IGNORECASE)


def parse_advisory_result(text: str) -> AdvisoryResult:
    """Parse ledger-ready advisory findings; legacy clean replies remain valid."""
    if _NO_FINDINGS.fullmatch(text or ""):
        return AdvisoryResult((), 0, "none")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CrossHarnessError("cross-harness reply is not structured JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"findings", "limitations"}:
        raise CrossHarnessError("cross-harness reply has an invalid object shape")
    findings = payload.get("findings")
    limitations = payload.get("limitations")
    if (
        not isinstance(findings, list)
        or len(findings) > 5
        or not all(isinstance(finding, dict) for finding in findings)
        or not isinstance(limitations, list)
        or len(limitations) > 3
        or not all(isinstance(item, str) for item in limitations)
    ):
        raise CrossHarnessError("cross-harness reply exceeds its signal budget")
    expected_fields = {"severity", "claim", "path", "line_start", "line_end"}
    severities = {"critical": 3, "important": 2, "suggestion": 1}
    for finding in findings:
        if (
            set(finding) != expected_fields
            or finding.get("severity") not in severities
            or not isinstance(finding.get("claim"), str)
            or not finding.get("claim")
            or finding.get("path") is not None
            and not isinstance(finding.get("path"), str)
            or any(
                value is not None
                and (isinstance(value, bool) or not isinstance(value, int) or value < 1)
                for value in (finding.get("line_start"), finding.get("line_end"))
            )
        ):
            raise CrossHarnessError("cross-harness finding is invalid")
    maximum = max(
        (str(finding["severity"]) for finding in findings),
        key=lambda severity: severities[severity],
        default="none",
    )
    return AdvisoryResult(tuple(findings), len(findings), maximum)


def parse_findings(text: str) -> tuple[int | None, str | None]:
    """Return (finding_count, max_severity) from an advisory pass's reply.

    A cross-harness pass runs only after local review has converged, so every
    finding it reports is a local-gate miss over a known denominator. Prefer the
    explicit trailer; an exact clean-pass phrase is the only fallback because
    severity words in prose do not reliably correspond one-to-one with findings.
    Returns (None, None) when neither source is trustworthy, so an unparsed reply
    is recorded as unknown rather than as zero.
    """
    if not text or not text.strip():
        return None, None
    try:
        structured = parse_advisory_result(text)
    except CrossHarnessError:
        pass
    else:
        return structured.finding_count, structured.max_severity
    if match := _FINDING_TRAILER.search(text):
        count = int(match.group(1))
        severity = match.group(2).lower()
        if (count == 0) != (severity == "none"):
            return None, None
        return count, severity
    if _NO_FINDINGS.fullmatch(text):
        return 0, "none"
    return None, None


def load_primary_chain_terminal(repo: Path, review_task_id: str) -> dict[str, object]:
    """Load one accepted, independently passed primary chain terminal."""
    authority_repo = primary_repo_root(repo)
    records = load_authority_records(authority_repo, WORK_UNIT_HISTORY_DAYS)
    terminal = latest_accepted_review_terminal(records, review_task_id)
    if terminal is None or not isinstance(terminal.get("review_chain_receipt"), dict):
        raise CrossHarnessError("primary review chain receipt is unavailable")
    verdict = authenticated_verdicts(
        records, _accepted_terminals={review_task_id: terminal}
    ).get(review_task_id)
    if verdict is None or verdict.get("verdict") != "pass":
        raise CrossHarnessError("primary review chain has no passing verdict")
    return terminal


def record_chain_advisory(
    *,
    repo: Path,
    primary_terminal: dict[str, object],
    skill: str,
    prompt: str,
    result_text: str,
) -> dict[str, object]:
    """Deposit advisory findings and append an immutable child receipt."""
    parsed = parse_advisory_result(result_text)
    primary = primary_terminal.get("review_chain_receipt")
    if not isinstance(primary, dict):
        raise CrossHarnessError("primary review chain receipt is unavailable")
    chain_id = str(primary.get("chain_id") or "")
    advisory_task_id = f"cross-harness-{chain_id.removeprefix('rc_')[:20]}"
    patch_identity = primary.get("patch_identity")
    snapshot = ReviewSnapshot(
        commit_sha=str(primary.get("snapshot_sha") or ""),
        tree_sha=str(primary.get("snapshot_tree_sha") or ""),
        directory=repo,
        patch_identity=(patch_identity if isinstance(patch_identity, dict) else None),
    )
    authority_repo = primary_repo_root(repo)
    source = primary_terminal.get("source_identity")
    try:
        capture = append_finding_records(
            authority_repo,
            task_id=advisory_task_id,
            result={"findings": list(parsed.findings)},
            snapshot=snapshot,
            worktree=repo,
            advisory=True,
            source_head=(
                str(source.get("head"))
                if isinstance(source, dict) and isinstance(source.get("head"), str)
                else None
            ),
            producer_skill=skill,
            category="cross-harness-review",
            producer_kind="local-review",
            unit_attempt_number=None,
        )
    except LedgerError as exc:
        raise CrossHarnessError(f"advisory finding capture failed: {exc}") from exc
    finding_ids = capture.get("finding_ids", []) if isinstance(capture, dict) else []
    try:
        receipt = build_review_chain_advisory_receipt(
            primary_receipt=primary,
            advisory_task_id=advisory_task_id,
            request_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            result_sha256=hashlib.sha256(result_text.encode()).hexdigest(),
            finding_ids=finding_ids,
        )
    except ReviewChainError as exc:
        raise CrossHarnessError(str(exc)) from exc
    append_authoritative_record(
        authority_repo,
        {
            "type": "review-chain-advisory",
            "status": "completed",
            "task_id": advisory_task_id,
            "primary_task_id": primary.get("task_id"),
            "advisory": True,
            "snapshot_sha": primary.get("snapshot_sha"),
            "snapshot_tree_sha": primary.get("snapshot_tree_sha"),
            "review_chain_advisory_receipt": receipt,
            "finding_capture_receipt": capture,
        },
        authority=create_coordinator_authority(),
        authority_kind="coordinator",
    )
    return receipt


def _claude_failure_reason(
    returncode: int,
    result: ClaudeResult | None,
) -> str:
    if returncode == 124:
        return "timeout"
    text = result.text.casefold() if result is not None else ""
    if "budget" in text:
        return "Claude budget exhausted"
    if "context" in text and any(
        marker in text for marker in ("limit", "window", "exceed", "too long")
    ):
        return "Claude context limit"
    if "rate limit" in text or "rate_limit" in text:
        return "Claude rate limited"
    if any(marker in text for marker in ("authentication", "not logged in")):
        return "Claude authentication failed"
    if result is not None and result.is_error:
        return "Claude returned an error"
    return f"Claude exited {returncode}"


def _scope_digest(scope_text: str) -> str:
    return hashlib.sha256(scope_text.encode()).hexdigest()[:16]


def _emit_event(
    repo: Path,
    *,
    skill: str,
    duration_ms: int,
    result: str,
    provider: str,
    scope_digest: str,
    cost_usd: float | None = None,
    budget_usd: float | None = None,
    prompt_chars: int | None = None,
    timeout_seconds: int | None = None,
    notes: str | None = None,
    finding_count: int | None = None,
    max_severity: str | None = None,
) -> None:
    event = {
        **kernel_events.common_fields(),
        "kind": "tool",
        "category": "cross-harness-review",
        "duration_ms": duration_ms,
        "result": result,
        "skill": skill,
        "provider": provider,
        "scope_digest": scope_digest,
    }
    for name, value in (
        ("cost_usd", cost_usd), ("budget_usd", budget_usd),
        ("prompt_chars", prompt_chars), ("timeout_seconds", timeout_seconds),
        ("finding_count", finding_count), ("max_severity", max_severity),
        ("notes", notes),
    ):
        if value is not None:
            event[name] = value
    try:
        kernel_events.write_event(event)
    except (OSError, ValueError) as exc:
        print(f"cross-harness telemetry warning: {exc}", file=sys.stderr)


def _prior_terminal_attempt(repo: Path, *, scope_digest: str) -> str | None:
    """Return the result of a terminal attempt for this exact scope, if any.

    Terminal per the rule: completed (pass/fail with findings surface) or a
    timeout/budget-exhausted skip. Availability/size skips are not attempts.
    """
    audit_dir = repo / ".audit" / "agent-events"
    if not audit_dir.is_dir():
        return None
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    terminal: str | None = None
    for log_path in sorted(audit_dir.glob("*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(event, dict)
                or event.get("kind") != "tool"
                or event.get("category") != "cross-harness-review"
                or event.get("scope_digest") != scope_digest
            ):
                continue
            try:
                ts = datetime.fromisoformat(
                    str(event.get("ts", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if ts < cutoff:
                continue
            result = str(event.get("result") or "")
            notes = str(event.get("notes") or "").split("; request: ", 1)[0].lower()
            if result in {"pass", "fail"} or (
                result == "skip"
                and (notes == "timeout" or "timed out" in notes or "budget" in notes)
            ):
                terminal = result
    return terminal


def _skip(
    repo: Path,
    *,
    skill: str,
    provider: str,
    scope_digest: str,
    duration_ms: int,
    reason: str,
    cost_usd: float | None = None,
    budget_usd: float | None = None,
    prompt_chars: int | None = None,
    timeout_seconds: int | None = None,
    request_reason: str | None = None,
) -> int:
    print(f"cross-harness pass skipped ({reason})", file=sys.stderr)
    _emit_event(
        repo,
        skill=skill,
        duration_ms=duration_ms,
        result="skip",
        provider=provider,
        scope_digest=scope_digest,
        cost_usd=cost_usd,
        budget_usd=budget_usd,
        prompt_chars=prompt_chars,
        timeout_seconds=timeout_seconds,
        notes=f"{reason}; request: {request_reason}" if request_reason else reason,
    )
    return 0


def _run_review_flow(
    runner: Any,
    *,
    repo: Path,
    skill: str,
    current_harness: str,
    scope: str,
    diff_range: str,
    paths: Sequence[str],
    new_gate: bool = False,
    review_task_id: str | None = None,
    request_reason: str | None = None,
    profile: Profile | None = None,
    registry: RuntimeRegistry = RUNTIME_REGISTRY,
    adapter_options: Mapping[str, Any] | None = None,
) -> int:
    """Run the preserved one-attempt advisory flow through a registered runner."""
    request_reason = (request_reason or "").strip()
    if not request_reason:
        print("cross-harness pass skipped (not explicitly requested)", file=sys.stderr)
        return 0
    if os.environ.get("CROSS_HARNESS_PASS"):
        print("cross-harness pass skipped (recursion guard)", file=sys.stderr)
        return 0
    if os.environ.get("AGENT_DISPATCH_DEPTH"):
        print("cross-harness pass skipped (dispatched worker)", file=sys.stderr)
        return 0
    if current_harness not in {"codex", "claude"}:
        raise CrossHarnessError(f"unsupported current harness: {current_harness}")

    primary_terminal: dict[str, object] | None = None
    if review_task_id is not None:
        primary_terminal = load_primary_chain_terminal(repo, review_task_id)
        primary_receipt = primary_terminal["review_chain_receipt"]
        assert isinstance(primary_receipt, dict)
        identity = primary_receipt.get("patch_identity")
        base_sha = identity.get("base_sha") if isinstance(identity, dict) else None
        snapshot_sha = primary_receipt.get("snapshot_sha")
        if not isinstance(base_sha, str) or not isinstance(snapshot_sha, str):
            raise CrossHarnessError("primary chain has no exact diff identity")
        scope = "diff"
        diff_range = f"{base_sha}..{snapshot_sha}"
        paths = []
    scope_text = collect_scope(
        runner, repo, scope=scope, diff_range=diff_range, paths=paths
    )
    digest = _scope_digest(scope_text)
    prior = _prior_terminal_attempt(repo, scope_digest=digest)
    if prior is not None and not new_gate:
        print(
            "cross-harness pass skipped (one attempt per logical gate — "
            f"terminal {prior} attempt already recorded for this exact scope "
            "in the last 24h; --new-gate overrides for a genuinely new gate)",
            file=sys.stderr,
        )
        return 0
    prompt = review_prompt(skill, scope, scope_text)
    prompt_chars = len(prompt)
    provider = "claude" if current_harness == "codex" else "codex"
    timeout_seconds = (
        CLAUDE_TIMEOUT_SECONDS if provider == "claude" else
        CODEX_REVIEW_TIMEOUT_SECONDS if scope == "diff" else
        CODEX_DOCUMENT_TIMEOUT_SECONDS
    )
    budget_usd = CLAUDE_BUDGET_USD if provider == "claude" else None
    if provider == "claude" and prompt_chars > CLAUDE_MAX_PROMPT_CHARS:
        return _skip(
            repo,
            request_reason=request_reason,
            skill=skill,
            provider=provider,
            scope_digest=digest,
            duration_ms=0,
            reason=(
                f"scope too large for Claude ({prompt_chars} chars > "
                f"{CLAUDE_MAX_PROMPT_CHARS} cap)"
            ),
            budget_usd=budget_usd,
            prompt_chars=prompt_chars,
            timeout_seconds=timeout_seconds,
        )
    configured = profile or _PROFILE.get()
    if configured is None:
        raise CrossHarnessError("cross-harness review requires a configured Profile")
    alias = configured.cross_harness_routes.get(current_harness)
    if alias is None or alias not in configured.aliases:
        raise CrossHarnessError("alternate review route is not configured")
    route = configured.aliases[alias]
    expected_runner = "claude" if current_harness == "codex" else "codex"
    if route.runner != expected_runner:
        raise CrossHarnessError("alternate review route does not cross harness families")
    provider = route.runner

    started = time.monotonic()
    try:
        registration = registry.registration(provider)
        adapter = registry.create(
            provider,
            settings=RuntimeSettings.from_profile(configured),
            **dict(adapter_options or {}),
        )
        request = RuntimeRequest(
            vendor=provider,
            transport=registration.preferred_transport,
            requested_model=route.model,
            effort="low" if provider == "claude" else route.default_effort,
            prompt=prompt,
            cwd=repo.resolve(),
            timeout_s=float(timeout_seconds),
            read_only=True,
            attempt_id=f"cross-harness-{digest}",
            tooling_root=configured.root.resolve(),
            eligibility=SubscriptionEligibility.APPROVED,
            commercial_mode=RuntimeCommercialMode.SUBSCRIPTION_ONLY,
            budget_usd=budget_usd,
            capability_profile=RuntimeCapabilityProfile(
                read_roots=(repo.resolve(),)
            ),
            visible_tools=(),
            allowed_tools=(),
        )
        readiness = adapter.probe(request)
        if not readiness.ready:
            reason = (
                readiness.failure.value
                if readiness.failure is not None
                else f"{provider} unavailable"
            )
            return _skip(
                repo,
                request_reason=request_reason,
                skill=skill,
                provider=provider,
                scope_digest=digest,
                duration_ms=0,
                reason=reason,
                budget_usd=budget_usd,
                prompt_chars=prompt_chars,
                timeout_seconds=timeout_seconds,
            )
        runtime_result = adapter.run(request)
    except (OSError, TypeError, ValueError) as exc:
        return _skip(
            repo,
            request_reason=request_reason,
            skill=skill,
            provider=provider,
            scope_digest=digest,
            duration_ms=int((time.monotonic() - started) * 1000),
            reason=str(exc),
            budget_usd=budget_usd,
            prompt_chars=prompt_chars,
            timeout_seconds=timeout_seconds,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    if runtime_result.status is not RuntimeStatus.COMPLETED:
        return _skip(
            repo,
            request_reason=request_reason,
            skill=skill,
            provider=provider,
            scope_digest=digest,
            duration_ms=duration_ms,
            reason=runtime_result.terminal_reason.value,
            cost_usd=runtime_result.cost_usd,
            budget_usd=budget_usd,
            prompt_chars=prompt_chars,
            timeout_seconds=timeout_seconds,
        )
    if runtime_result.structured_output is not None:
        result_text = json.dumps(runtime_result.structured_output)
    else:
        result_text = (runtime_result.final_output or "").strip()
    if not result_text:
        return _skip(
            repo,
            request_reason=request_reason,
            skill=skill,
            provider=provider,
            scope_digest=digest,
            duration_ms=duration_ms,
            reason=f"{provider} returned empty output",
        )
    print(result_text)
    finding_count, max_severity = parse_findings(result_text)
    if primary_terminal is not None:
        try:
            receipt = record_chain_advisory(
                repo=repo,
                primary_terminal=primary_terminal,
                skill=skill,
                prompt=prompt,
                result_text=result_text,
            )
        except CrossHarnessError as exc:
            return _skip(
                repo,
                request_reason=request_reason,
                skill=skill,
                provider=provider,
                scope_digest=digest,
                duration_ms=duration_ms,
                reason=str(exc),
            )
        finding_count = len(receipt["finding_ids"])
        max_severity = parse_advisory_result(result_text).max_severity
    _emit_event(
        repo,
        skill=skill,
        duration_ms=duration_ms,
        result="pass",
        notes=f"request: {request_reason}",
        provider=provider,
        scope_digest=digest,
        cost_usd=runtime_result.cost_usd,
        budget_usd=budget_usd,
        prompt_chars=prompt_chars,
        timeout_seconds=timeout_seconds,
        finding_count=finding_count,
        max_severity=max_severity,
    )
    return 0


def run_review(runner_or_profile: Any, **kwargs: Any) -> int | AdvisoryResult:
    """Support the moved flow and the package's normalized direct adapter seam."""
    # The moved command flow always supplies a worktree.  Keep the small
    # package adapter structural so tests and embedders may provide an
    # immutable Profile-shaped object without importing the concrete class.
    if "worktree" not in kwargs:
        return _run_review_flow(runner_or_profile, **kwargs)

    from .evidence import _validate_result_findings

    profile = runner_or_profile
    worktree = Path(kwargs.pop("worktree"))
    alias = str(kwargs.pop("alias"))
    effort = str(kwargs.pop("effort"))
    prompt = str(kwargs.pop("prompt"))
    attempt_id = str(kwargs.pop("attempt_id"))
    registry = kwargs.pop("registry", RUNTIME_REGISTRY)
    adapter_options = dict(kwargs.pop("adapter_options", {}) or {})
    output_schema = kwargs.pop("output_schema", None)
    if kwargs:
        raise TypeError(f"unexpected run_review arguments: {sorted(kwargs)}")
    routing.configure(profile)
    decision = routing.route(alias, effort)
    try:
        registration = registry.registration(decision.engine)
        adapter = registry.create(
            decision.engine,
            settings=RuntimeSettings.from_profile(profile),
            **adapter_options,
        )
        request = RuntimeRequest(
            vendor=decision.engine,
            transport=registration.preferred_transport,
            requested_model=decision.model,
            effort=effort,
            prompt=prompt,
            cwd=worktree.resolve(),
            timeout_s=float(profile.default_timeout_s),
            read_only=True,
            attempt_id=attempt_id,
            tooling_root=profile.root.resolve(),
            eligibility=SubscriptionEligibility.APPROVED,
            commercial_mode=RuntimeCommercialMode.SUBSCRIPTION_ONLY,
            budget_usd=profile.routing_budgets.get(effort),
            output_schema=output_schema,
            capability_profile=RuntimeCapabilityProfile(
                read_roots=(worktree.resolve(),)
            ),
            visible_tools=(),
            allowed_tools=(),
        )
        readiness = adapter.probe(request)
        if not readiness.ready:
            reason = readiness.failure.value if readiness.failure else "unavailable"
            raise CrossHarnessError(f"review runtime is not ready: {reason}")
        result = adapter.run(request)
    except (TypeError, ValueError) as exc:
        raise CrossHarnessError(
            f"configured review runtime is unavailable: {exc}"
        ) from exc
    if result.status is not RuntimeStatus.COMPLETED:
        raise CrossHarnessError(
            f"review runtime did not complete: {result.terminal_reason.value}"
        )
    payload: object = result.structured_output
    if payload is None:
        try:
            payload = json.loads(result.final_output or "")
        except json.JSONDecodeError as exc:
            raise CrossHarnessError("review runtime result is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise CrossHarnessError("review runtime result must be an object")
    findings = _validate_result_findings(payload.get("findings"))
    severities = {"suggestion": 1, "important": 2, "critical": 3}
    maximum = max(
        (str(row["severity"]) for row in findings),
        key=severities.get,
        default="none",
    )
    return AdvisoryResult(tuple(findings), len(findings), maximum, result)

# CLI composition stays in the consumer.
