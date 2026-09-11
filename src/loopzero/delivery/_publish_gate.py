"""Finding-trailer publication helpers extracted from the excluded merge gate."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Sequence

from ..integrations.github import GateError
from ..kernel.git_config_security import security_projection_sha256
from ..kernel.trusted_exec import system_executable


FINDING_ID_RE = re.compile(r"f_[0-9a-f]{20}")
EXACT_FIXES_TRAILER_RE = re.compile(r"Fixes: (f_[0-9a-f]{20})")
_GIT_TRAILER_RE = re.compile(r"[A-Za-z0-9-]+:\s+\S.*")
FINDING_FIXES_LIKE_RE = re.compile(r"^\s*fixes\s*:\s*f_", re.IGNORECASE)


def _inspect_finding_id_lines(
    message: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    finding_ids: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line in message.split("\n"):
        match = EXACT_FIXES_TRAILER_RE.fullmatch(line)
        if match is not None:
            finding_id = match.group(1)
            if finding_id not in seen:
                seen.add(finding_id)
                finding_ids.append(finding_id)
            continue
        if FINDING_FIXES_LIKE_RE.match(line):
            errors.append(f"malformed Finding Ledger trailer: {line!r}")
    return tuple(finding_ids), tuple(errors)


def extract_exact_finding_ids(message: str) -> tuple[str, ...]:
    finding_ids, errors = _inspect_finding_id_lines(message)
    if errors:
        raise GateError(errors[0])
    return finding_ids


def inspect_exact_finding_ids(
    messages: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    finding_ids: list[str] = []
    errors: list[str] = []
    for message in messages:
        message_ids, message_errors = _inspect_finding_id_lines(message)
        finding_ids.extend(message_ids)
        errors.extend(message_errors)
    return tuple(dict.fromkeys(finding_ids)), tuple(errors)


def collect_exact_finding_ids(messages: Iterable[str]) -> tuple[str, ...]:
    finding_ids, errors = inspect_exact_finding_ids(messages)
    if errors:
        raise GateError(errors[0])
    return finding_ids


def format_orphan_finding_warning(
    finding_ids: Iterable[str], known_finding_ids: Iterable[str]
) -> str | None:
    known = set(known_finding_ids)
    orphaned = tuple(
        dict.fromkeys(
            finding_id for finding_id in finding_ids if finding_id not in known
        )
    )
    if not orphaned:
        return None
    return (
        "exact Fixes trailers name Finding Ledger IDs absent from the canonical "
        f"ledger: {', '.join(orphaned)}. Deposit valid provenance before merge "
        "when readily available, or remove an incorrect trailer."
    )


def compose_squash_body(body: str, finding_ids: Sequence[str]) -> str:
    existing = extract_exact_finding_ids(body)
    deduplicated = tuple(dict.fromkeys((*existing, *finding_ids)))
    for finding_id in deduplicated:
        if FINDING_ID_RE.fullmatch(finding_id) is None:
            raise GateError(f"invalid Finding Ledger ID for squash: {finding_id!r}")
    content_lines = [
        line
        for line in body.splitlines()
        if EXACT_FIXES_TRAILER_RE.fullmatch(line) is None
    ]
    content = "\n".join(content_lines).strip()
    trailers = "\n".join(f"Fixes: {finding_id}" for finding_id in deduplicated)
    if content and trailers:
        final_paragraph = content.rsplit("\n\n", maxsplit=1)[-1]
        separator = (
            "\n"
            if final_paragraph
            and all(
                _GIT_TRAILER_RE.fullmatch(line) for line in final_paragraph.splitlines()
            )
            else "\n\n"
        )
        return f"{content}{separator}{trailers}"
    return content or trailers
