"""Generate or update index/README files from frontmatter metadata.

Supports two modes:
  - Full-file generation (simple directories)
  - Managed-section generation (preserves manual content around generated region)

Managed sections are delimited by marker comments:
  <!-- .generated-index-start -->
  ... generated content ...
  <!-- .generated-index-end -->

Usage:
  python3 scripts/docs/generate_indexes.py              # Generate all
  python3 scripts/docs/generate_indexes.py --check      # Check for drift (CI)
  python3 scripts/docs/generate_indexes.py --dir <path> # Single directory
  python3 scripts/docs/generate_indexes.py --quiet      # Suppress summary output (pre-commit)
  python3 scripts/docs/generate_indexes.py --list-targets
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:  # Supports both direct scripts and package-based unit tests.
    from ._frontmatter import parse_frontmatter as parse_typed_frontmatter
    from .docs_lifecycle_policy import (
        is_active_document,
        is_historical_copy,
        load_policy,
    )
except ImportError:  # pragma: no cover - direct-script path
    from _frontmatter import parse_frontmatter as parse_typed_frontmatter
    from docs_lifecycle_policy import (
        is_active_document,
        is_historical_copy,
        load_policy,
    )

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
POLICY = load_policy()

MARKER_START = "<!-- .generated-index-start -->"
MARKER_END = "<!-- .generated-index-end -->"
INDEX_FILENAMES = {"index.md", "INDEX.md", "README.md"}
EXCLUDED_INDEX_PATHS = {
    "docs/templates/README.md",
    "docs/design/specs/INDEX.md",
    "docs/design/specs/README.md",
}
SCAN_MODE_OVERRIDES = {
    "docs": "child_indexes",
    "docs/design": "direct_and_child_indexes",
    "docs/features": "child_indexes",
    "docs/design/blueprints": "child_indexes",
    "docs/design/initiatives": "child_indexes",
    "docs/design/initiatives/active": "child_indexes",
    "docs/design/initiatives/completed": "child_indexes",
    "docs/guides": "recursive",
    "docs/ops": "child_indexes",
    "docs/architecture": "direct_and_child_indexes",
    "docs/components": "direct_and_child_indexes",
}

# ── Frontmatter parser ────────────────────────────────────────────────


def parse_frontmatter_bounds(text: str) -> tuple[int, int] | None:
    """Return byte offsets for frontmatter content between --- markers."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    return (4, end)


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse metadata and reject fields that cannot safely form an index entry."""
    try:
        data, _ = parse_typed_frontmatter(text)
    except (AttributeError, TypeError) as exc:
        raise ValueError("invalid frontmatter metadata") from exc
    if "path" in data:
        raise ValueError("frontmatter field 'path' is reserved for the source file")
    for field in ("name", "title", "filename", "link", "status", "severity", "doc_type"):
        if field in data and not isinstance(data[field], str):
            raise ValueError(f"frontmatter field '{field}' must be a string")
    return data


def extract_title(text: str) -> str:
    """Extract first H1 from the document body, not frontmatter comments."""
    bounds = parse_frontmatter_bounds(text)
    body = text if bounds is None else text[bounds[1] + len("\n---\n") :]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def read_doc_entry(md_file: Path) -> dict[str, Any] | None:
    """Read one markdown file into an index entry."""
    text = md_file.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    if not fm:
        return None
    return {
        "path": md_file,
        "name": md_file.stem,
        "title": extract_title(text),
        "filename": md_file.name,
        **fm,
    }


def add_relative_links(
    directory: Path, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach a relative markdown link target for each entry."""
    linked_entries: list[dict[str, Any]] = []
    for entry in entries:
        relative_path = entry["path"].relative_to(directory).as_posix()
        linked_entries.append({**entry, "link": f"./{relative_path}"})
    return linked_entries


def scan_directory(directory: Path, index_names: set[str] | None = None) -> list[dict[str, Any]]:
    """Scan a directory for .md files (excluding index/README), parse frontmatter."""
    if index_names is None:
        index_names = INDEX_FILENAMES
    entries = []
    for md_file in sorted(directory.glob("*.md")):
        if md_file.name in index_names:
            continue
        if is_historical_copy(
            md_file, POLICY, root=ROOT
        ) or not is_active_document(md_file, POLICY, root=ROOT):
            continue
        entry = read_doc_entry(md_file)
        if entry:
            entries.append(entry)
    return entries


def scan_recursive(directory: Path, index_names: set[str] | None = None) -> list[dict[str, Any]]:
    """Scan a directory tree for .md files (excluding index/README), parse frontmatter."""
    if index_names is None:
        index_names = INDEX_FILENAMES
    entries = []
    for md_file in sorted(directory.rglob("*.md")):
        if md_file.name in index_names:
            continue
        if is_historical_copy(
            md_file, POLICY, root=ROOT
        ) or not is_active_document(md_file, POLICY, root=ROOT):
            continue
        entry = read_doc_entry(md_file)
        if entry:
            entries.append(entry)
    return entries


def scan_child_indexes(directory: Path) -> list[dict[str, Any]]:
    """Scan immediate child directories for their own index/README files."""
    entries = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        for index_name in ("index.md", "INDEX.md", "README.md"):
            candidate = child / index_name
            if candidate.exists():
                rel = str(candidate.relative_to(ROOT))
                if rel in EXCLUDED_INDEX_PATHS:
                    break
                if is_historical_copy(
                    candidate, POLICY, root=ROOT
                ) or not is_active_document(candidate, POLICY, root=ROOT):
                    break
                entry = read_doc_entry(candidate)
                if entry:
                    entries.append(entry)
                break
    return entries


def collect_entries(directory: Path, index_name: str, scan_mode: str) -> list[dict[str, Any]]:
    """Collect entries for one generated index target."""
    direct = scan_directory(directory, INDEX_FILENAMES | {index_name})
    if scan_mode == "direct":
        return add_relative_links(directory, direct)
    if scan_mode == "child_indexes":
        return add_relative_links(directory, scan_child_indexes(directory))
    if scan_mode == "direct_and_child_indexes":
        child_entries = scan_child_indexes(directory)
        seen_paths = {entry["path"] for entry in direct}
        direct.extend(
            entry for entry in child_entries if entry["path"] not in seen_paths
        )
        return add_relative_links(directory, direct)
    if scan_mode == "recursive":
        return add_relative_links(directory, scan_recursive(directory, INDEX_FILENAMES | {index_name}))
    return add_relative_links(directory, direct)


# ── Directory templates ───────────────────────────────────────────────

def _date_from_name(name: str) -> str:
    """Extract YYYY-MM-DD from filename prefix."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else ""


def template_learnings(entries: list[dict[str, Any]]) -> str:
    """Status-grouped table for learnings directory."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        status = e.get("status", "unknown")
        groups.setdefault(status, []).append(e)

    lines: list[str] = []
    status_order = ["open", "proposed", "implemented", "archived"]
    for status in status_order:
        items = groups.get(status, [])
        if not items:
            continue
        lines.append(f"### {status.capitalize()} ({len(items)})")
        lines.append("")
        lines.append("| Date | Learning | Severity |")
        lines.append("|------|----------|----------|")
        for e in sorted(items, key=lambda x: _date_from_name(x["name"]), reverse=True):
            date = _date_from_name(e["name"])
            link = f"[{e['title'] or e['name']}]({e['link']})"
            severity = e.get("severity", "—")
            lines.append(f"| {date} | {link} | {severity} |")
        lines.append("")

    if not lines:
        lines.append("*No learnings found.*")
        lines.append("")
    return "\n".join(lines)


def template_features(entries: list[dict[str, Any]]) -> str:
    """Registry table for feature directories."""
    lines = ["| Feature | Status | Type |", "|---------|--------|------|"]
    for e in sorted(entries, key=lambda x: x.get("title", x["name"])):
        link = f"[{e['title'] or e['name']}]({e['link']})"
        status = e.get("status", "—")
        doc_type = e.get("doc_type", "—")
        lines.append(f"| {link} | {status} | {doc_type} |")
    if len(lines) == 2:
        return "*No documents found.*\n"
    lines.append("")
    return "\n".join(lines)


def template_dated_docs(entries: list[dict[str, Any]]) -> str:
    """Table for date-prefixed docs (explorations, specs, blueprints)."""
    lines = ["| Date | Document | Status |", "|------|----------|--------|"]
    for e in sorted(entries, key=lambda x: _date_from_name(x["name"]), reverse=True):
        date = _date_from_name(e["name"])
        link = f"[{e['title'] or e['name']}]({e['link']})"
        status = e.get("status", "—")
        lines.append(f"| {date} | {link} | {status} |")
    if len(lines) == 2:
        return "*No documents found.*\n"
    lines.append("")
    return "\n".join(lines)


def template_adrs(entries: list[dict[str, Any]]) -> str:
    """Table for ADRs."""
    lines = ["| Date | ADR | Status |", "|------|-----|--------|"]
    for e in sorted(entries, key=lambda x: _date_from_name(x["name"]), reverse=True):
        date = _date_from_name(e["name"])
        link = f"[{e['title'] or e['name']}]({e['link']})"
        status = e.get("status", "—")
        lines.append(f"| {date} | {link} | {status} |")
    if len(lines) == 2:
        return "*No ADRs found.*\n"
    lines.append("")
    return "\n".join(lines)


def template_scenarios(entries: list[dict[str, Any]]) -> str:
    """Table for scenario files."""
    lines = ["| Scenario File | Status |", "|---------------|--------|"]
    for e in sorted(entries, key=lambda x: x["name"]):
        link = f"[{e['title'] or e['name']}]({e['link']})"
        status = e.get("status", "—")
        lines.append(f"| {link} | {status} |")
    if len(lines) == 2:
        return "*No scenarios found.*\n"
    lines.append("")
    return "\n".join(lines)


def template_default(entries: list[dict[str, Any]]) -> str:
    """Simple alphabetical list."""
    lines = []
    for e in sorted(entries, key=lambda x: x.get("title", x["name"])):
        link = f"[{e['title'] or e['name']}]({e['link']})"
        status = e.get("status", "")
        suffix = f" ({status})" if status else ""
        lines.append(f"- {link}{suffix}")
    if not lines:
        return "*No documents found.*\n"
    lines.append("")
    return "\n".join(lines)


# ── Directory → template mapping ──────────────────────────────────────

def pick_template(rel_dir: str) -> Any:
    """Choose a template function based on the directory path."""
    if "ops/learnings" in rel_dir:
        return template_learnings
    if "features/" in rel_dir or rel_dir.endswith("features"):
        return template_features
    if "architecture/adrs" in rel_dir:
        return template_adrs
    if "scenarios" in rel_dir:
        return template_scenarios
    if "explorations" in rel_dir or "specs" in rel_dir or "blueprints/" in rel_dir:
        return template_dated_docs
    return template_default


# ── Index file registry ───────────────────────────────────────────────

def discover_managed_indexes() -> list[dict[str, str]]:
    """Discover all index-like docs targets covered by DSS Phase 3."""
    targets: list[dict[str, str]] = []
    candidate_paths = sorted(
        DOCS_DIR.rglob("*.md"),
        key=lambda path: (-len(path.relative_to(ROOT).parts), str(path)),
    )
    for index_path in candidate_paths:
        if index_path.name not in INDEX_FILENAMES:
            continue
        rel_path = str(index_path.relative_to(ROOT))
        if rel_path in EXCLUDED_INDEX_PATHS:
            continue
        if is_historical_copy(index_path, POLICY, root=ROOT):
            continue
        if not is_active_document(index_path, POLICY, root=ROOT):
            continue
        rel_dir = str(index_path.parent.relative_to(ROOT))
        targets.append(
            {
                "rel_dir": rel_dir,
                "index_name": index_path.name,
                "mode": "managed",
                "scan_mode": SCAN_MODE_OVERRIDES.get(rel_dir, "direct"),
            }
        )
    return targets


# ── Core logic ────────────────────────────────────────────────────────

def inject_managed_section(existing_content: str, generated: str) -> str:
    """Replace content between markers, or append markers + content at end."""
    start_idx = existing_content.find(MARKER_START)
    end_idx = existing_content.find(MARKER_END)

    section = f"{MARKER_START}\n\n{generated}\n{MARKER_END}\n"

    if start_idx != -1 and end_idx != -1:
        # Replace existing managed section
        return existing_content[:start_idx] + section + existing_content[end_idx + len(MARKER_END) + 1:]
    else:
        # Append managed section
        content = existing_content.rstrip("\n")
        return f"{content}\n\n{section}"


def process_directory(
    directory: Path,
    index_name: str,
    mode: str,
    *,
    scan_mode: str,
    check: bool = False,
) -> bool:
    """Process a single directory. Returns True if up-to-date (or updated)."""
    if not directory.is_dir():
        return True

    # Opt-out marker
    if (directory / ".no-generate-index").exists():
        return True

    index_path = directory / index_name
    entries = collect_entries(directory, index_name, scan_mode)

    rel_dir = str(directory.relative_to(ROOT))
    template_fn = pick_template(rel_dir)
    generated = template_fn(entries)

    if mode == "managed":
        if not index_path.exists():
            # Nothing to inject into
            return True
        existing = index_path.read_text(encoding="utf-8")
        new_content = inject_managed_section(existing, generated)
    else:
        # Full mode — not used currently but kept for extensibility
        new_content = generated

    if check:
        if not index_path.exists():
            print(f"  MISSING: {index_path.relative_to(ROOT)}")
            return False
        current = index_path.read_text(encoding="utf-8")
        if current != new_content:
            print(f"  DRIFT: {index_path.relative_to(ROOT)}")
            return False
        return True
    else:
        if index_path.exists() and index_path.read_text(encoding="utf-8") == new_content:
            return True  # No change needed
        index_path.write_text(new_content, encoding="utf-8")
        print(f"  Updated: {index_path.relative_to(ROOT)}")
        return True


def generate_all(
    *,
    check: bool = False,
    single_dir: Path | None = None,
) -> tuple[bool, int]:
    """Process managed indexes for CLI and composing maintenance workflows."""
    all_ok = True
    processed = 0
    for target in discover_managed_indexes():
        directory = ROOT / target["rel_dir"]
        if single_dir and directory != single_dir.resolve():
            continue
        processed += 1
        if not process_directory(
            directory,
            target["index_name"],
            target["mode"],
            scan_mode=target["scan_mode"],
            check=check,
        ):
            all_ok = False
    return all_ok, processed


def main() -> None:
    check = "--check" in sys.argv
    quiet = "--quiet" in sys.argv
    list_targets = "--list-targets" in sys.argv
    single_dir = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--dir" and i < len(sys.argv) - 1:
            single_dir = sys.argv[i + 1]

    if list_targets:
        for target in discover_managed_indexes():
            print(f"{target['rel_dir']}/{target['index_name']}")
        return

    all_ok, processed = generate_all(
        check=check,
        single_dir=Path(single_dir) if single_dir else None,
    )

    if quiet:
        if check and not all_ok:
            sys.exit(1)
        return

    if check:
        if all_ok:
            print(f"✅ All managed index sections are up to date ({processed} checked)")
        else:
            print(f"\n❌ Index drift detected. Run: python3 scripts/docs/generate_indexes.py")
            sys.exit(1)
    else:
        if not single_dir:
            print(f"✅ Index generation complete ({processed} directories)")


if __name__ == "__main__":
    main()
