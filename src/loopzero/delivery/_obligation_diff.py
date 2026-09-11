"""Detect weakened or removed obligations in a skill/rule file, deterministically.

Why this exists: a model asked to notice a missing word in a 1.1 MB corpus
detects single-word obligation erosion about **a third of the time** — measured
across three runs and two engines on 2026-07-26 (RF-48). That is the instrument's
resolution floor, not a defect to remediate. Detecting that a word vanished is
mechanical work; models should judge what a change *means*.

So this does the mechanical half at 100%:

    "- [ ] Findings are evidence-backed, ranked, and routed."
    "- [ ] Findings are ranked and routed."
                      ^^^ obligation weakened, flagged

An obligation is a line that imposes a requirement — a checklist item, or a
sentence carrying a modal ("must", "never", "do not", "always", "only", …).
Obligations are matched between versions by similarity, then compared word by
word, so a retained-but-weakened line is caught rather than looking unchanged.

This flags weakening; it cannot know whether a weakening is *intended*. Callers
need an acknowledgment path, or the output becomes noise that gets rubber-stamped
(named risk, RF-49).
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Words that turn a sentence into a requirement. Deliberately conservative:
# a false negative here is a missed obligation, so the escape hatch below
# (`<!-- obligation -->`) exists for informally-phrased ones.
_MODALS = re.compile(
    r"\b(must not|must|shall not|shall|never|do not|don't|always|only|"
    r"required|require|requires|forbidden|prohibited|may not|cannot)\b",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*[-*+]\s+\S")
_CHECKLIST = re.compile(r"^\s*[-*]\s*\[[ xX]?\]\s+(?P<text>.+?)\s*$")
_ANNOTATED = re.compile(r"<!--\s*obligation\s*-->")
_CODE_FENCE = re.compile(r"^\s*```")

# Qualifiers whose removal weakens a requirement without changing its shape.
# These are what the model floor misses.
_QUALIFIERS = frozenset(
    {
        "evidence-backed", "independently", "explicitly", "exactly", "never",
        "always", "must", "only", "verified", "atomic", "atomically", "prove",
        "proven", "required", "immutable", "deterministic", "before", "first",
    }
)


@dataclass(frozen=True)
class Obligation:
    text: str
    line_no: int

    @property
    def words(self) -> list[str]:
        return re.findall(r"[\w'-]+", self.text.lower())


@dataclass(frozen=True)
class Finding:
    kind: str  # "removed" | "weakened"
    before: str
    after: str | None
    detail: str

    def render(self) -> str:
        if self.kind == "removed":
            return f"  REMOVED  {self.before}\n           ({self.detail})"
        return f"  WEAKENED {self.before}\n        -> {self.after}\n           ({self.detail})"


def extract(text: str) -> list[Obligation]:
    """Return the obligations in a document, skipping fenced code."""
    out: list[Obligation] = []
    in_fence = False
    for i, raw in enumerate(text.splitlines(), start=1):
        if _CODE_FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        checklist = _CHECKLIST.match(raw)
        if checklist:
            out.append(Obligation(checklist.group("text"), i))
            continue
        # Every bullet in a skill or rule is normative. Grammar-based extraction
        # (modals only) missed 3 of 8 planted canaries on 2026-07-26 because real
        # obligations are written as imperatives ("Record confirmed removals…")
        # and routing declaratives ("… routes to `execute-blueprint`"), not as
        # "must" sentences. Recall matters more than precision here: the diff
        # itself filters, since an unchanged obligation is never reported.
        if _BULLET.match(raw) or _ANNOTATED.search(line) or _MODALS.search(line):
            out.append(Obligation(re.sub(r"^\s*[-*+]\s*", "", line), i))
    return out


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def compare(before_text: str, after_text: str, *, threshold: float = 0.6) -> list[Finding]:
    """Report obligations removed outright or retained but weakened."""
    before = extract(before_text)
    after = extract(after_text)
    after_pool = list(after)
    findings: list[Finding] = []

    for old in before:
        exact = next((n for n in after_pool if n.text.strip() == old.text.strip()), None)
        if exact is not None:
            after_pool.remove(exact)
            continue

        best, score = None, 0.0
        for cand in after_pool:
            s = _similarity(old.text, cand.text)
            if s > score:
                best, score = cand, s

        if best is None or score < threshold:
            findings.append(
                Finding("removed", old.text, None, f"line {old.line_no}, no counterpart")
            )
            continue

        after_pool.remove(best)
        lost = [w for w in old.words if w not in best.words]
        if not lost:
            continue
        qualifiers = [w for w in lost if w in _QUALIFIERS]
        detail = (
            f"lost qualifier(s): {', '.join(qualifiers)}"
            if qualifiers
            else f"lost word(s): {', '.join(lost[:6])}"
        )
        findings.append(Finding("weakened", old.text, best.text, detail))

    return findings


_ACK = re.compile(
    r"^[ \t]*Obligation-change:[ \t]*(?P<reason>\S.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
# An unfilled template placeholder is not a reason.
_ACK_PLACEHOLDER = re.compile(r"^[<\[{(]|^(reason|todo|tbd|n/?a|why)\b", re.IGNORECASE)


def acknowledgment_reason(text: str) -> str | None:
    """Return a substantive `Obligation-change:` reason, if one is present."""
    match = _ACK.search(text or "")
    if not match:
        return None
    reason = match.group("reason").strip()
    if _ACK_PLACEHOLDER.match(reason) or len(reason) < 10:
        return None
    return reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--acknowledgment",
        help="text carrying an `Obligation-change: <reason>` line (commit message "
             "or PR body). Intentional weakening is acknowledged, never silenced.",
    )
    args = parser.parse_args(argv)

    findings = compare(args.before.read_text(), args.after.read_text())
    if not findings:
        print("obligation diff: no obligation removed or weakened")
        return 0

    print(f"obligation diff: {len(findings)} obligation(s) affected")
    for f in findings:
        print(f.render())

    reason = acknowledgment_reason(args.acknowledgment or "")
    if reason:
        # Acknowledged, not silenced: the finding still prints, the reason is
        # recorded beside it, and `Obligation-change:` stays greppable so the
        # rate can be measured. A check that can be turned off invisibly is a
        # check that will be.
        print(f"\nACKNOWLEDGED: {reason}")
        return 0

    print("\nIf this weakening is intentional, add to the commit message or PR body:")
    print("    Obligation-change: <why this obligation may be weakened>")
    print("Do not silence the check by editing it away.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
