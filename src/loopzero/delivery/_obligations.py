"""Block silent weakening of agent-config obligations in a PR.

A model asked to notice a missing word in a 1.1 MB corpus detects single-word
obligation erosion about a third of the time (RF-48, measured over three runs and
two engines). This does the same job deterministically at 100%, in milliseconds:

    - [ ] Findings are evidence-backed, ranked, and routed.
    - [ ] Findings are ranked and routed.
                      ^^^ blocked unless acknowledged

Scope note that matters (RF-49): this is a **per-change** check. Replayed across
two weeks of history it flags 29 of 35 skills, because legitimate rewrites
restructure text wholesale. Against a single PR diff it reports 0-3 findings.
Point it at a release-sized range and it will look broken.

Weakening is *acknowledged*, never silenced: the finding still prints, the reason
prints beside it, and `Obligation-change:` stays greppable so the acknowledgment
rate can be measured. A check that can be disabled invisibly is one that will be.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ._obligation_diff import acknowledgment_reason, compare
from ._body_check import visible_contract_text

# Agent-config surfaces whose bullets are normative. Docs and code are excluded:
# a prose guide weakening a sentence is not the failure mode this guards.
WATCHED = (".cursor/skills/", ".cursor/rules/")
SUFFIXES = (".md", ".mdc")


class ObligationCheckError(RuntimeError):
    """Raised when the exact comparison range cannot be proven."""


@dataclass(frozen=True, slots=True)
class FileChange:
    """One watched path transition in a base...head diff."""

    status: str
    before_path: str | None
    after_path: str | None

    @property
    def display_path(self) -> str:
        if self.before_path and self.after_path and self.before_path != self.after_path:
            return f"{self.before_path} -> {self.after_path}"
        return self.before_path or self.after_path or "<unknown>"


def _watched(path: str) -> bool:
    return path.startswith(WATCHED) and path.endswith(SUFFIXES)


def _run_git(args: list[str], *, check: bool, runner=None):
    if runner is not None:
        return runner.run(args, check=check)
    try:
        return subprocess.run(args, capture_output=True, text=True, check=check)
    except (OSError, subprocess.CalledProcessError) as exc:
        operation = " ".join(args[1:3])
        raise ObligationCheckError(
            f"git {operation} failed while checking obligation changes"
        ) from exc


def merge_base(base: str, head: str, *, runner=None) -> str:
    proc = _run_git(
        ["git", "merge-base", base, head],
        check=True,
        runner=runner,
    )
    revision = proc.stdout.strip()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ObligationCheckError("git merge-base returned an invalid commit ID")
    return revision


def changed_files(base: str, head: str, *, runner=None) -> list[FileChange]:
    out = _run_git(
        ["git", "diff", "--name-status", "-M", "-z", f"{base}...{head}"],
        check=True,
        runner=runner,
    ).stdout.split("\0")
    changes: list[FileChange] = []
    index = 0
    while index < len(out) and out[index]:
        status = out[index]
        index += 1
        if status.startswith(("R", "C")):
            before_path, after_path = out[index], out[index + 1]
            index += 2
            if status.startswith("C"):
                # A copy leaves the source obligations in place, so only the
                # destination can be a watched addition.
                if _watched(after_path):
                    changes.append(FileChange(status, None, after_path))
                continue
            watched_before = before_path if _watched(before_path) else None
            watched_after = after_path if _watched(after_path) else None
            if watched_before or watched_after:
                changes.append(FileChange(status, watched_before, watched_after))
            continue

        path = out[index]
        index += 1
        if not _watched(path):
            continue
        if status.startswith("A"):
            changes.append(FileChange(status, None, path))
        elif status.startswith("D"):
            changes.append(FileChange(status, path, None))
        else:
            changes.append(FileChange(status, path, path))
    return changes


def _show(revision: str, path: str, *, runner=None) -> str | None:
    proc = _run_git(["git", "show", f"{revision}:{path}"], check=False, runner=runner)
    return proc.stdout if proc.returncode == 0 else None


def check_obligation_changes(
    base: str,
    head: str,
    *,
    acknowledgment_text: str = "",
    runner=None,
) -> int:
    """Run the shared CI/publication policy against one exact Git range."""
    comparison_base = (
        merge_base(base, head, runner=runner)
        if runner is not None
        else merge_base(base, head)
    )
    files = (
        changed_files(comparison_base, head, runner=runner)
        if runner is not None
        else changed_files(comparison_base, head)
    )
    if not files:
        print("obligation check: no agent-config file changed")
        return 0

    affected: list[tuple[str, list]] = []
    for change in files:
        before = (
            (
                _show(comparison_base, change.before_path, runner=runner)
                if runner is not None
                else _show(comparison_base, change.before_path)
            )
            if change.before_path is not None
            else None
        )
        after = (
            (
                _show(head, change.after_path, runner=runner)
                if runner is not None
                else _show(head, change.after_path)
            )
            if change.after_path is not None
            else None
        )
        if change.before_path is not None and before is None:
            raise ObligationCheckError(
                "cannot read merge-base version of watched file: "
                f"{change.before_path}"
            )
        if change.after_path is not None and after is None:
            raise ObligationCheckError(
                f"cannot read head version of watched file: {change.after_path}"
            )
        if before is None:
            continue
        findings = compare(before, after or "")
        if findings:
            affected.append((change.display_path, findings))

    if not affected:
        print(
            f"obligation check: {len(files)} agent-config file(s) changed, "
            "no obligation removed or weakened"
        )
        return 0

    total = sum(len(findings) for _, findings in affected)
    print(f"obligation check: {total} obligation(s) weakened or removed")
    for path, findings in affected:
        print(f"\n{path}")
        for finding in findings:
            print(finding.render())

    reason = acknowledgment_reason(visible_contract_text(acknowledgment_text))
    if reason:
        print(f"\nACKNOWLEDGED: {reason}")
        return 0

    print("\n::error::agent-config obligations were weakened without acknowledgment")
    print("If this is intentional, add to the PR body:")
    print("    Obligation-change: <why this obligation may be weakened>")
    print("Do not resolve this by editing the check.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--acknowledgment-file",
        type=Path,
        help="file holding the PR body or commit message to scan for the trailer",
    )
    args = parser.parse_args(argv)

    ack_text = ""
    if args.acknowledgment_file and args.acknowledgment_file.exists():
        ack_text = args.acknowledgment_file.read_text()
    try:
        return check_obligation_changes(
            args.base,
            args.head,
            acknowledgment_text=ack_text,
        )
    except ObligationCheckError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
