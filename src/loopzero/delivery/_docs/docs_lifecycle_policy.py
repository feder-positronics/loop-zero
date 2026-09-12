"""Shared policy helpers for active documentation scopes.

The freeze policy is deliberately subtractive: consumers start from their
existing scope and call :func:`is_active_document` before doing work.  An
explicit policy marker is the only local escape hatch for a frozen artifact.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = Path(__file__).resolve().with_name("docs-lifecycle.yaml")


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    """Load the JSON-formatted lifecycle policy."""
    return json.loads(path.read_text(encoding="utf-8"))


def archive_manifest_path(
    root: Path = ROOT,
    policy: dict[str, Any] | None = None,
) -> Path:
    """Resolve the transient archive manifest from the lifecycle policy."""
    active_policy = policy or load_policy()
    return root / str(active_policy["archive"]["manifest_output"])


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Read lifecycle metadata with the repository's shared YAML parser."""
    try:
        from ._frontmatter import parse_frontmatter as parse_markdown_frontmatter
    except ImportError:  # pragma: no cover - direct-script path
        from _frontmatter import parse_frontmatter as parse_markdown_frontmatter

    frontmatter, _ = parse_markdown_frontmatter(text)
    return frontmatter


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def relative_path(path: Path, root: Path = ROOT) -> str:
    """Normalize repository paths while leaving external test trees unmatched."""
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        # Test or caller-owned trees outside the repository cannot match a
        # repository-relative lifecycle path.
        return path.as_posix()


def is_policy_excluded(path: Path, policy: dict[str, Any], root: Path = ROOT) -> bool:
    """Return whether a path is excluded by the existing policy scope."""
    rel = relative_path(path, root)
    patterns = policy["validation"]["exclude_paths"]
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)


def is_historical_copy(
    path: Path, policy: dict[str, Any], root: Path = ROOT
) -> bool:
    """Return whether a path is a moved copy excluded from active sweeps."""
    rel = relative_path(path, root)
    archive_root = str(policy["archive"]["tombstone_root"]).rstrip("/")
    return rel == archive_root or rel.startswith(f"{archive_root}/")


def is_archive_tombstone(
    text: str, policy_or_prefix: dict[str, Any] | str
) -> bool:
    """Return whether a document body is the one-line archive redirect."""
    end = text.find("\n---\n", 4)
    if end == -1:
        return False
    body = text[end + 5 :].strip()
    if body.startswith("# "):
        body = body[2:]
    prefix = (
        str(policy_or_prefix["archive"]["tombstone_prefix"])
        if isinstance(policy_or_prefix, dict)
        else policy_or_prefix
    )
    return body.startswith(prefix)


def has_unfreeze_marker(text: str, policy: dict[str, Any]) -> bool:
    marker = policy.get("freeze", {}).get("unfreeze_marker")
    return bool(marker and marker in text)


def is_frozen_document(
    path: Path,
    policy: dict[str, Any],
    *,
    root: Path = ROOT,
    today: date | None = None,
    text: str | None = None,
) -> bool:
    """Return whether the policy makes an existing document immutable.

    A marker is intentionally checked before lifecycle metadata so an owner can
    unfreeze a document in the same edit that otherwise would be rejected.
    """
    if not path.exists() and text is None:
        return False
    freeze = policy.get("freeze")
    if not isinstance(freeze, dict):
        return False
    contents = text if text is not None else path.read_text(encoding="utf-8")
    if has_unfreeze_marker(contents, policy):
        return False
    frontmatter = parse_frontmatter(contents)
    doc_type = str(frontmatter.get("doc_type", ""))
    status = str(frontmatter.get("status", ""))
    last_updated = parse_date(str(frontmatter.get("last_updated", "")))
    if last_updated is None:
        return False

    matches_rule = any(
        fnmatch.fnmatch(relative_path(path, root), str(rule.get("path", "")))
        and rule.get("doc_type") == doc_type
        and status in rule.get("statuses", [])
        for rule in freeze["rules"]
    )
    if not matches_rule:
        return False
    as_of = today or datetime.now(UTC).date()
    return (as_of - last_updated).days >= int(freeze["minimum_age_days"])


def is_active_document(
    path: Path,
    policy: dict[str, Any],
    *,
    root: Path = ROOT,
    today: date | None = None,
) -> bool:
    """Return whether a document remains in freeze-sensitive tool scopes.

    Existing static exclusions stay with the consumers that already owned
    them. This predicate subtracts only paths made terminal by ``freeze`` so
    adopting it cannot silently widen a consumer's historical exclusions.
    """
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if is_archive_tombstone(text, policy):
            return False
        return not is_frozen_document(
            path, policy, root=root, today=today, text=text
        )
    return not is_frozen_document(path, policy, root=root, today=today)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--active-paths",
        nargs="*",
        required=True,
        metavar="PATH",
        help="Print only supplied paths that are in the active policy scope.",
    )
    args = parser.parse_args()
    policy = load_policy()
    for raw_path in args.active_paths:
        path = ROOT / raw_path
        if path.exists() and is_active_document(path, policy):
            print(raw_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
