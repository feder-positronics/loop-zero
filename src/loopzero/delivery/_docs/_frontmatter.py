"""Shared YAML frontmatter helpers for docs automation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _parse_simple_frontmatter(raw: str) -> dict[str, Any]:
    """Parse legacy frontmatter that predates strict YAML formatting."""
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("- ", "* ")) and current_key:
            data.setdefault(current_key, []).append(stripped[2:].strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value == "null":
            data[current_key] = None
        elif value == "[]":
            data[current_key] = []
        elif value:
            data[current_key] = value
        else:
            data[current_key] = []
    return data


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return frontmatter mapping and markdown body."""
    if not text.startswith("---\n"):
        return {}, text

    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text

    raw = text[4:end]
    try:
        frontmatter = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        frontmatter = _parse_simple_frontmatter(raw)
    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must parse to a mapping")
    return frontmatter, text[end + 5 :]


def format_frontmatter(frontmatter: dict[str, Any]) -> str:
    """Format frontmatter as a YAML document block."""
    payload = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{payload}\n---\n"


def read_markdown(path: Path) -> tuple[dict[str, Any], str]:
    """Read a markdown file and split frontmatter from body."""
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def write_markdown(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    """Write a markdown file from frontmatter and body."""
    path.write_text(format_frontmatter(frontmatter) + body, encoding="utf-8")
