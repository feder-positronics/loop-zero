"""Validate IntelFlo's complete PR-body contract before PR mutation or CI."""

from __future__ import annotations

import argparse
import html
import re
import subprocess
from pathlib import Path

from ..integrations.github import GitHub, GitHubError, GitHubSettings

ISSUE_REFERENCE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|revert(?:s|ed|ing)?)"
    r"\s+#[0-9]+",
    re.IGNORECASE,
)
STANDALONE_REASON_RE = re.compile(
    r"^[ \t]*Standalone-Reason:[ \t]+\S.*$",
    re.IGNORECASE | re.MULTILINE,
)
CONTEXT_AND_GOAL_RE = re.compile(
    r"^## Context and goal[ \t]*$" r"(?P<body>.*?)" r"(?=^## [^#]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
CONTEXT_FIELD_RE = re.compile(
    r"^[ ]{0,3}-[ \t]+\*\*(?P<name>Context|Problem|Goal):\*\*"
    r"[ \t]+(?P<value>\S.*)$",
    re.IGNORECASE | re.MULTILINE,
)
GLOSSARY_RE = re.compile(
    r"^### Glossary[ \t]*$(?P<body>.*?)(?=^#{2,3}[ \t]+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
GLOSSARY_TERM_RE = re.compile(
    r"^[ ]{0,3}-[ \t]+\*\*(?P<term>[^*\n:]+):\*\*[ \t]+\S.*$",
    re.MULTILINE,
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_ELEMENT_RE = re.compile(r"</?[A-Za-z][^>]*>")
MARKDOWN_FENCE_RE = re.compile(r"^[ ]{0,3}(?:`{3,}|~{3,})", re.MULTILINE)
MARKDOWN_INDENTED_FIELD_RE = re.compile(
    r"^(?: {4,}|\t+)[ ]*-[ \t]+\*\*(?:Context|Problem|Goal):\*\*",
    re.IGNORECASE | re.MULTILINE,
)
FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
H2_SECTION_RE = re.compile(
    r"^##[ \t]+(?P<name>[^\n]+?)[ \t]*$" r"(?P<body>.*?)" r"(?=^##[ \t]+|\Z)",
    re.MULTILINE | re.DOTALL,
)
MARKDOWN_LINK_DEFINITION_RE = re.compile(
    r"^[ ]{0,3}\[[^\]\n]+\]:[ \t]+\S.*$", re.MULTILINE
)
PLACEHOLDER_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?"
    r"(?:(?:result|validation|verification|tests?|checks?)[ \t]*:[ \t]*)?"
    r"(?:"
    r"<[^>\r\n]+>|"
    r"(?:todo|tbd|pending|none|n/?a|not[ \t]+(?:run|tested|executed|applicable))"
    r"(?:[ \t]*(?:[.!]|[:\-\u2014][^\r\n]*))?"
    r")[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# One importable, deterministic contract owns the template, local checks,
# automated publication, adoption, and trusted-base CI checks. Optional entries
# are documented here so callers do not infer that all template headings are
# mandatory.
PR_BODY_CONTRACT: dict[str, int | tuple[str, ...]] = {
    "version": 1,
    "required_sections": ("Context and goal", "Validation"),
    "required_context_fields": ("Context", "Problem", "Goal"),
    "optional_sections": ("Glossary", "Decisions taken"),
}


def _without_inline_code(line: str) -> str:
    """Remove complete Markdown code spans while preserving line structure."""
    visible: list[str] = []
    cursor = 0
    while cursor < len(line):
        if line[cursor] != "`":
            visible.append(line[cursor])
            cursor += 1
            continue
        run_end = cursor
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        marker = line[cursor:run_end]
        close = line.find(marker, run_end)
        if close < 0:
            visible.append(marker)
            cursor = run_end
            continue
        visible.append(" " * (close + len(marker) - cursor))
        cursor = close + len(marker)
    return "".join(visible)


def visible_contract_text(body: str) -> str:
    """Return prose that can legitimately satisfy the PR-body contract."""
    normalized = HTML_COMMENT_RE.sub("", body).replace("\r\n", "\n").replace("\r", "\n")
    visible: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in normalized.splitlines(keepends=True):
        candidate = line.rstrip("\n")
        opening = FENCE_OPEN_RE.match(candidate)
        if fence_character is None and opening is not None:
            marker = opening.group("fence")
            fence_character = marker[0]
            fence_length = len(marker)
            visible.append("\n" if line.endswith("\n") else "")
            continue
        if fence_character is not None:
            close = re.match(
                rf"^[ \t]{{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$",
                candidate,
            )
            if close is not None:
                fence_character = None
                fence_length = 0
            visible.append("\n" if line.endswith("\n") else "")
            continue
        visible.append(_without_inline_code(line))
    return "".join(visible)


def _validate_html_comments(body: str) -> str | None:
    """Reject comment openers that make GitHub hide the rest of the body."""
    cursor = 0
    while (opening := body.find("<!--", cursor)) >= 0:
        closing = body.find("-->", opening + 4)
        if closing < 0:
            return "Unclosed HTML comment hides required PR body content."
        cursor = closing + 3
    return None


def has_issue_reference(body: str) -> bool:
    return ISSUE_REFERENCE_RE.search(visible_contract_text(body)) is not None


def has_standalone_reason(body: str) -> bool:
    """Return whether the initial PR body declares a non-empty exemption reason."""
    return STANDALONE_REASON_RE.search(visible_contract_text(body)) is not None


def _section_body(body: str, name: str) -> str | None:
    visible_body = visible_contract_text(body)
    for section in H2_SECTION_RE.finditer(visible_body):
        if section.group("name").strip().casefold() == name.casefold():
            return section.group("body")
    return None


def validate_required_sections(body: str) -> str | None:
    """Require every contract section and meaningful Validation content."""
    validation = _section_body(body, "Validation")
    if validation is None:
        return "Missing required '## Validation' section."
    validation_without_placeholders = PLACEHOLDER_LINE_RE.sub("", validation)
    if not has_visible_text(validation_without_placeholders):
        return "Required '## Validation' section must contain a result."
    return None


def contains_complete_phrase(text: str, phrase: str) -> bool:
    """Return whether phrase appears without adjoining word characters."""
    normalized_phrase = phrase.strip().casefold()
    return (
        re.search(
            rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)",
            text.casefold(),
        )
        is not None
    )


def _without_markdown_link_destinations(value: str) -> str:
    """Keep rendered link labels while removing hidden destination text."""
    visible: list[str] = []
    cursor = 0
    while cursor < len(value):
        label_start = cursor + 1 if value.startswith("![", cursor) else cursor
        if label_start >= len(value) or value[label_start] != "[":
            visible.append(value[cursor])
            cursor += 1
            continue
        label_end = label_start + 1
        bracket_depth = 1
        while label_end < len(value) and bracket_depth:
            if value[label_end] == "\\":
                label_end += 2
                continue
            if value[label_end] == "[":
                bracket_depth += 1
            elif value[label_end] == "]":
                bracket_depth -= 1
            label_end += 1
        if bracket_depth:
            visible.append(value[cursor])
            cursor += 1
            continue
        label = value[label_start + 1 : label_end - 1]
        destination_start = label_end
        if destination_start < len(value) and value[destination_start] == "(":
            destination_end = destination_start + 1
            parenthesis_depth = 1
            while destination_end < len(value) and parenthesis_depth:
                if value[destination_end] == "\\":
                    destination_end += 2
                    continue
                if value[destination_end] == "(":
                    parenthesis_depth += 1
                elif value[destination_end] == ")":
                    parenthesis_depth -= 1
                destination_end += 1
            if parenthesis_depth == 0:
                visible.append(label)
                cursor = destination_end
                continue
        if destination_start < len(value) and value[destination_start] == "[":
            reference_end = value.find("]", destination_start + 1)
            if reference_end >= 0:
                visible.append(label)
                cursor = reference_end + 1
                continue
        visible.append(value[cursor])
        cursor += 1
    return "".join(visible)


def has_visible_text(value: str) -> bool:
    """Return whether an HTML-decoded field contains a visible word character."""
    without_definitions = MARKDOWN_LINK_DEFINITION_RE.sub("", value)
    without_destinations = _without_markdown_link_destinations(without_definitions)
    return any(character.isalnum() for character in html.unescape(without_destinations))


def validate_context_and_goal(body: str) -> str | None:
    """Require the plain-language opening used by ready PR publication."""
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n")
    comment_stripped_body = HTML_COMMENT_RE.sub("", normalized_body)
    if "<!--" in comment_stripped_body:
        return "Context and goal contains an unclosed HTML comment."
    visible_body = visible_contract_text(normalized_body)
    match = CONTEXT_AND_GOAL_RE.search(visible_body)
    if match is None:
        return "Missing required '## Context and goal' opening."
    if visible_body[: match.start()].strip():
        return "'## Context and goal' must be the first PR body content."
    section_body = match.group("body")
    if HTML_ELEMENT_RE.search(section_body):
        return "Context and goal must not contain HTML elements."
    if MARKDOWN_INDENTED_FIELD_RE.search(section_body):
        return "Context and goal must not contain Markdown-indented required fields."
    glossary = GLOSSARY_RE.search(section_body)
    opening_body = (
        section_body[: glossary.start()] if glossary is not None else section_body
    )
    fields = {
        field.group("name").casefold(): field.group("value")
        for field in CONTEXT_FIELD_RE.finditer(opening_body)
    }
    missing = [
        name
        for name in ("Context", "Problem", "Goal")
        if name.casefold() not in fields
        or not has_visible_text(fields[name.casefold()])
    ]
    if missing:
        raw_match = CONTEXT_AND_GOAL_RE.search(comment_stripped_body)
        if raw_match is not None and MARKDOWN_FENCE_RE.search(raw_match.group("body")):
            return "Context and goal must not contain Markdown code fences."
        return "Context and goal has missing or empty field(s): " + ", ".join(missing)
    if glossary is not None:
        opening = "\n".join(fields.values()).casefold()
        absent = [
            term.group("term").strip()
            for term in GLOSSARY_TERM_RE.finditer(glossary.group("body"))
            if not contains_complete_phrase(opening, term.group("term"))
        ]
        if absent:
            return (
                "Glossary term(s) absent from Context, Problem, or Goal: "
                + ", ".join(absent)
            )
    return None


def validate(body: str, *, draft: bool = False, standalone: bool = False) -> str | None:
    if draft:
        return None
    if standalone or has_standalone_reason(body):
        return None
    if has_issue_reference(body):
        return None
    return (
        "No work reference found. Add 'Closes #N', 'Refs #N', or "
        "'Reverts #N'; otherwise add 'Standalone-Reason: <reason>' for "
        "deliberately issue-less work."
    )


def validate_contract(
    body: str, *, draft: bool = False, standalone: bool = False
) -> str | None:
    """Validate format for every PR and linkage for ready PRs."""
    if error := _validate_html_comments(body):
        return error
    if error := validate_context_and_goal(body):
        return error
    if error := validate_required_sections(body):
        return error
    return validate(body, draft=draft, standalone=standalone)


def _pr_target(pr: str) -> tuple[str, str]:
    if re.fullmatch(r"[1-9][0-9]*", pr):
        return pr, "repos/{owner}/{repo}"
    match = re.fullmatch(
        r"https://github\.com/"
        r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/"
        r"(?P<repo>[A-Za-z0-9_.-]+)/pull/"
        r"(?P<number>[1-9][0-9]*)/?",
        pr,
    )
    if match is None:
        raise ValueError(f"cannot determine GitHub PR identity from {pr}")
    return match.group("number"), f"repos/{match.group('owner')}/{match.group('repo')}"


def load_pr(
    pr: str, *, github: GitHub | None = None
) -> tuple[str, bool, bool, str, str]:
    number, requested_repository = _pr_target(pr)
    client = github or GitHub(
        Path.cwd(), GitHubSettings(labels={"standalone": "standalone"})
    )
    try:
        bound_repository = f"repos/{client.repository().slug}"
        if requested_repository != "repos/{owner}/{repo}" and (
            requested_repository != bound_repository
        ):
            raise GitHubError("PR URL does not match the repository-bound origin")
        payload = client.api(f"{bound_repository}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError("GitHub returned malformed PR metadata")
    except GitHubError as exc:
        raise ValueError(f"cannot load PR metadata for {pr}") from exc
    labels = payload.get("labels") or []
    standalone = any(label.get("name") == "standalone" for label in labels)
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    base_oid = str(base.get("sha") or "")
    head_oid = str(head.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", base_oid) or not re.fullmatch(
        r"[0-9a-f]{40}", head_oid
    ):
        raise ValueError(f"PR {pr} has invalid base or head identity")
    return (
        str(payload.get("body") or ""),
        bool(payload.get("draft")),
        standalone,
        base_oid,
        head_oid,
    )


def _has_commit(oid: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{oid}^{{commit}}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ValueError("cannot inspect local PR commits") from exc
    return completed.returncode == 0


def materialize_pr_commits(pr: str, base_oid: str, head_oid: str) -> None:
    """Fetch the live PR range when either exact commit is absent locally."""
    if _has_commit(base_oid) and _has_commit(head_oid):
        return
    number, _repository = _pr_target(pr)
    try:
        subprocess.run(
            [
                "git",
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                base_oid,
                f"refs/pull/{number}/head",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot fetch live commits for PR {pr}") from exc
    if not _has_commit(base_oid) or not _has_commit(head_oid):
        raise ValueError(f"live commits for PR {pr} are unavailable after fetch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--body-file", type=Path)
    source.add_argument("--pr", help="PR number or URL to validate after creation")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument(
        "--ready",
        action="store_true",
        help=(
            "Remove only the draft exemption before marking a PR ready; a live "
            "standalone label remains valid"
        ),
    )
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--base", help="merge-base input for obligation checking")
    parser.add_argument("--head", help="exact head input for obligation checking")
    args = parser.parse_args(argv)

    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be supplied together")
    if args.pr and args.base:
        parser.error("--base and --head cannot be combined with --pr")

    if args.pr:
        try:
            body, draft, standalone, range_base, range_head = load_pr(args.pr)
        except ValueError as exc:
            parser.error(str(exc))
        if args.ready:
            draft = False
    else:
        body = args.body_file.read_text(encoding="utf-8")
        draft = args.draft
        standalone = args.standalone
        range_base = args.base
        range_head = args.head

    if error := validate_contract(body, draft=draft, standalone=standalone):
        parser.error(error)
    if args.pr:
        try:
            materialize_pr_commits(args.pr, range_base, range_head)
        except ValueError as exc:
            parser.error(str(exc))
    if range_base:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from check_obligation_changes import (
            ObligationCheckError,
            check_obligation_changes,
        )

        try:
            obligation_result = check_obligation_changes(
                range_base,
                range_head,
                acknowledgment_text=body,
            )
        except ObligationCheckError as exc:
            parser.error(str(exc))
        if obligation_result:
            parser.error(
                "agent-config obligation changes require a substantive acknowledgment"
            )
    print("PR body contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
