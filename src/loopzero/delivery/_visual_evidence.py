"""Require evidence that a changed UI surface was actually rendered and driven.

Why this exists: on 2026-07-25 three UI features shipped broken past a fully
converged gate stack — a source-quality filter that rendered as a non-working
number field, an Inquiries flow where the only way to run the agent was a
"Synthesis" button inside a "Synthesis" tab, and entity chips that reset the
view to inbox while displaying `MESH:D012345` instead of the entity name. Every
gate passed. `fe.mdc` already required visual evidence, but as an unenforced
markdown checkbox.

Two-tier contract (PIL-VISUAL-DEMOTE-1 + PIL-VISUAL-RETARGET-1, 2026-08-28):
missing evidence on a changed surface is **advisory** — a visible warning,
never a block — except when an **added ``app/**/page.tsx``** (a brand
new surface) ships without evidence, which fails the check. The check accepts
three things, in order of strength:

1. **Journey evidence** — a Playwright spec under ``nextjs-frontend/__tests__``
   was added or changed. This is the load-bearing one: all three defects above
   were behavioural, and a screenshot alone would have caught only the ugly one.
2. **Visual evidence** — an image embedded in the PR body.
3. **A declared bypass** — ``Visual-evidence-bypass: <reason>``. Deliberately
   countable: ``gh pr list --search "Visual-evidence-bypass"`` measures how
   often the check is wrong, so it can be tuned on evidence rather than opinion.

Only ``.tsx`` files under ``app/`` and ``components/`` trigger it — the actual
rendering surface. Tests, stories, and non-rendering ``.ts`` are not triggers,
which keeps the false-positive rate low enough that the bypass stays meaningful.

**Known limitation, accepted deliberately.** The gate does not verify that the
changed spec covers the changed surface, so an unrelated spec edit satisfies it.
Closing that needs a surface-to-spec ownership mapping, which is a larger design
than the gate is worth today. This is a *floor* that replaces an unenforced
checkbox, with `code-review` as the ceiling; the bypass rate and any observed
gaming are the evidence that would justify tightening it. Do not mistake a green
check here for "this UI was verified".
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

TRIGGER_DIRS = ("nextjs-frontend/app/", "nextjs-frontend/components/")
SPEC_DIR = "nextjs-frontend/__tests__/"
_NON_TRIGGER = ("__tests__/", ".test.", ".spec.", ".stories.")

_BYPASS = re.compile(
    r"^[ \t]*Visual-evidence-bypass:[ \t]*(?P<reason>\S.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# An unfilled template placeholder is not a declared bypass.
_PLACEHOLDER = re.compile(r"^[<\[{(]|^(reason|todo|tbd|n/?a)\b", re.IGNORECASE)
# Markdown image, bare image URL, or a GitHub attachment (user-images/assets).
_IMAGE = re.compile(
    r"!\[[^\]]*\]\([^)]+\)"
    r"|<img\s[^>]*src=",
    re.IGNORECASE,
)
_IMAGE_URL = re.compile(
    r"https?://\S+?(?:\.png|\.jpe?g|\.gif|\.webp)(?:\?\S*)?"
    r"|https?://(?:user-images\.githubusercontent\.com|github\.com/user-attachments)/\S+",
    re.IGNORECASE,
)
# Decorative images are the real false-evidence vector. Requiring a dedicated
# "## Visual evidence" section would be stricter, but a backtest over 60 merged
# PRs (2026-07-25) showed 21 of 35 UI PRs satisfy the gate with an unsectioned
# screenshot; demanding a section would push the failure rate from 17% to ~77%
# and the gate would be bypassed reflexively — which is the checkbox it replaces.
_IMAGE_SCAN_LIMIT = 100_000
_DECORATIVE = re.compile(
    r"shields\.io|badge|\.svg|codecov\.io|img\.shields|travis-ci|circleci\.com",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Result:
    ok: bool
    reason: str
    bypass_reason: str | None = None
    # Two-tier contract (PIL-VISUAL-RETARGET-1, 2026-08-28, owner-directed):
    # missing evidence is advisory except for a NEW surface — an added
    # `app/**/page.tsx` must ship with a journey spec (or a counted bypass).
    blocking: bool = False

    @property
    def bypassed(self) -> bool:
        return self.bypass_reason is not None


def is_trigger(path: str) -> bool:
    """True when a path is a rendering surface whose change needs evidence."""
    if not path.endswith(".tsx"):
        return False
    if any(marker in path for marker in _NON_TRIGGER):
        return False
    return path.startswith(TRIGGER_DIRS)


def is_journey_evidence(path: str) -> bool:
    return path.startswith(SPEC_DIR) and (
        path.endswith(".spec.ts") or path.endswith(".spec.tsx")
    )


def has_image(body: str) -> bool:
    """True when the body embeds an image that is not obviously decorative."""
    # Bound the regex surface: PR bodies are attacker-influenceable and the
    # image patterns run several passes; 100k chars is far beyond any real body.
    body = body[:_IMAGE_SCAN_LIMIT]
    candidates = [m.group(0) for m in _IMAGE.finditer(body)]
    candidates += [m.group(0) for m in _IMAGE_URL.finditer(body)]
    # A markdown image match stops at ')', so re-check the whole line for
    # badge markers that live in the surrounding link wrapper.
    lines = [ln for ln in body.splitlines() if _IMAGE.search(ln) or _IMAGE_URL.search(ln)]
    return any(not _DECORATIVE.search(line) for line in lines) and bool(candidates)


def is_new_surface(path: str) -> bool:
    """True when an added path introduces a whole new page surface."""
    return (
        path.startswith("nextjs-frontend/app/")
        and path.endswith("/page.tsx")
        and is_trigger(path)
    )


def evaluate(
    changed_files: list[str],
    pr_body: str,
    added_files: list[str] | None = None,
) -> Result:
    body = pr_body or ""
    triggers = [p for p in changed_files if is_trigger(p)]

    if not triggers:
        return Result(True, "no rendering surface changed")

    # The bypass is checked before evidence so a declared bypass is always
    # recorded as such, rather than being masked by incidental evidence.
    bypass = _BYPASS.search(body)
    if bypass and not _PLACEHOLDER.match(bypass.group("reason")):
        return Result(
            True,
            f"bypass declared for {len(triggers)} changed surface(s)",
            bypass_reason=bypass.group("reason"),
        )

    specs = [p for p in changed_files if is_journey_evidence(p)]
    if specs:
        return Result(True, f"journey evidence: {len(specs)} Playwright spec(s) changed")

    if has_image(body):
        return Result(True, "visual evidence: image embedded in PR body")

    # Callers without add/modify knowledge (added_files=None) stay advisory;
    # only a caller that can prove a surface is NEW may raise the blocking tier.
    new_surfaces = sorted(p for p in (added_files or []) if is_new_surface(p))
    listed = "\n".join(f"    {p}" for p in sorted(triggers)[:10])
    more = "" if len(triggers) <= 10 else f"\n    … {len(triggers) - 10} more"
    if new_surfaces:
        return Result(
            False,
            "NEW page surface(s) added without a journey spec:\n"
            + "\n".join(f"    {p}" for p in new_surfaces)
            + "\n  (a new route must ship with a Playwright spec that drives it,"
            "\n   or a counted Visual-evidence-bypass reason)",
            blocking=True,
        )
    clipped = (
        "\n  (note: body exceeded the 100k-char image-scan bound; evidence "
        "past the cap is not seen)"
        if len(body) > _IMAGE_SCAN_LIMIT
        else ""
    )
    return Result(
        False,
        "changed rendering surfaces without visual or journey evidence:\n"
        f"{listed}{more}{clipped}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files",
        help="path to a newline-delimited file list; '-' reads stdin",
        default="-",
    )
    parser.add_argument(
        "--body-env",
        default="PR_BODY",
        help="env var holding the PR body (never pass the body as an argument)",
    )
    parser.add_argument(
        "--added-files",
        default=None,
        help=(
            "optional path to a newline-delimited list of ADDED files; "
            "enables the blocking new-surface tier"
        ),
    )
    args = parser.parse_args(argv)

    if args.changed_files == "-":
        raw = sys.stdin.read()
    else:
        with open(args.changed_files, encoding="utf-8") as handle:
            raw = handle.read()
    changed = [line.strip() for line in raw.splitlines() if line.strip()]

    added: list[str] | None = None
    if args.added_files is not None:
        with open(args.added_files, encoding="utf-8") as handle:
            added = [line.strip() for line in handle if line.strip()]

    result = evaluate(changed, os.environ.get(args.body_env, ""), added)

    if result.bypassed:
        print(f"⚠️  Visual evidence BYPASSED — {result.reason}")
        print(f"    reason: {result.bypass_reason}")
        print("    Bypasses are counted: gh pr list --search 'Visual-evidence-bypass'")
        return 0
    if result.ok:
        print(f"✅ Visual evidence check passed — {result.reason}")
        return 0

    if not result.blocking:
        # Advisory tier (PIL-VISUAL-DEMOTE-1): visible, never gating.
        print(f"::warning::{result.reason.splitlines()[0]}")
        print(result.reason)
        print()
        print("Advisory: prefer a Playwright spec, a screenshot in the PR body, or")
        print("run `make visual-evidence` locally to capture the changed surfaces.")
        return 0

    print(f"::error::{result.reason.splitlines()[0]}")
    print(result.reason)
    print()
    print("A NEW page surface must ship one of:")
    print("  • a Playwright spec under nextjs-frontend/__tests__/ that drives the")
    print("    new journey (strongest — catches behaviour, not just looks);")
    print("  • a screenshot embedded in the PR body;")
    print("  • 'Visual-evidence-bypass: <reason>' in the PR body (counted).")
    print()
    print("Rationale: fe.mdc § Blocking Rules; two-tier contract")
    print("PIL-VISUAL-RETARGET-1 in docs/design/process-improvement-history.md.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
