"""Command interface and concise advisory report."""

import json
import subprocess
from pathlib import Path, PurePosixPath

from .analyzers import RUFF_VERSIONS, VERSIONS
from .collector import collect


def markdown(report):
    lines = [
        "# Code structure evidence",
        "",
        "Advisory candidates; source inspection determines action.",
        "",
        f"Collection: {'complete' if report['valid'] else 'INCOMPLETE'}",
        f"Source: `{report['head']['sha']}`",
        f"Policy: `{report['policy_id']}`",
        "",
    ]
    if "base" in report:
        lines.append(f"Base: `{report['base']['sha']}`")
    for category, total in report["head"]["totals"].items():
        lines.append(
            f"- {category}: {total['files']} files, {total['lines']} physical lines"
        )
    if "comparison" in report:
        leads = report["comparison"]["highlights"]
    else:
        leads = [
            {"kind": "function-span", **f}
            for f in sorted(report["head"]["functions"], key=lambda f: -f["span"])[:3]
        ]
        lines.append(
            f"\nClone families: {sum(len(d['families']) for d in report['head']['duplication'].values())}"
        )
    lines.append("\nReview leads (see JSON for full inventory and coverage):")
    for lead in leads:
        if "locations" in lead:
            membership_labels = {
                True: "seen in base",
                False: "new path",
                None: "base match uncertain",
            }
            locations = ", ".join(
                f"`{loc['path']}:{loc['line']}` "
                f"({membership_labels[loc.get('fragment_seen_in_base')]})"
                for loc in lead["locations"]
            )
            lines.append(
                f"- Duplication candidate: {locations}. Verify shared behavior in source."
            )
        elif "before" in lead:
            lines.append(
                f"- `{lead['path']}:{lead['line']}` — `{lead['name']}` "
                f"{lead['kind']}: {lead['before']} → {lead['after']}."
            )
        else:
            lines.append(
                f"- `{lead['path']}:{lead['line']}` — `{lead['name']}`: "
                f"{lead['span']} lines, complexity {lead['complexity']}."
            )
    coverage = report["head"]["coverage"]
    lines.append(
        f"\nCoverage: {coverage['complexity_measured']}/{coverage['python_functions']} "
        f"Python functions have complexity values; {len(coverage['complexity_unsupported'])} "
        f"files have unsupported complexity; {len(report['head']['excluded'])} paths excluded."
    )
    if "comparison" in report:
        lines.append(
            f"Unmatched head functions: {len(report['comparison']['unmatched_head_functions'])}; "
            "these need source review and are not asserted regressions."
        )
    if not leads and report["valid"]:
        lines.append(
            "- No matched new or worsened leads; inspect unmatched and coverage in JSON."
        )
    for key in ("base", "head"):
        if key in report:
            lines.extend(
                f"\nCollection error ({key}): {error}"
                for error in report[key]["errors"]
            )
    return "\n".join(lines) + "\n"


def run(args):
    policy = {
        "scope": sorted(set(args.scope)),
        "exclude": sorted(set(args.exclude)),
        "test_root": sorted(set(args.test_root)),
        "test_pattern": sorted(set(args.test_pattern)),
        "ruff_version": args.ruff_version,
        "signals": sorted(set(args.signals.split(",")) | {"size"}),
        "min_lines": args.min_lines,
        "min_tokens": args.min_tokens,
        "clone_mode": "weak",
    }
    try:
        for root in policy["scope"] + policy["test_root"]:
            if root != "." and (
                PurePosixPath(root).is_absolute()
                or ".." in PurePosixPath(root).parts
                or str(PurePosixPath(root)) != root
            ):
                raise ValueError(
                    f"scope roots must be normalized repository-relative paths: {root}"
                )
        if not set(policy["signals"]) <= {"size", "complexity", "duplication"}:
            raise ValueError("signals must be size,complexity,duplication")
        if min(args.min_lines, args.min_tokens) < 1:
            raise ValueError("clone thresholds must be positive")
        report = collect(args.root, args.head, policy, getattr(args, "base", None))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        report = {
            "schema_version": 1,
            "valid": False,
            "threshold_failed": False,
            "errors": [str(exc)],
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    if args.report:
        Path(args.report).write_text(
            markdown(report)
            if "head" in report
            else "INCOMPLETE: " + str(report["errors"]) + "\n",
            encoding="utf-8",
        )
    return 0 if report["valid"] else 1


def register(sub):
    commands = sub.add_parser(
        "code-health", help="advisory committed-source structural evidence"
    ).add_subparsers(dest="health_command", required=True)
    for mode in ("snapshot", "compare"):
        parser = commands.add_parser(mode)
        parser.add_argument(
            "--head", required=True, help="explicit commit/ref; never the working tree"
        )
        if mode == "compare":
            parser.add_argument("--base", required=True)
        parser.add_argument("--scope", action="append", required=True)
        parser.add_argument("--exclude", action="append", default=[])
        parser.add_argument("--test-root", action="append", default=[])
        parser.add_argument(
            "--test-pattern",
            action="append",
            default=[],
            help="fnmatch pattern for colocated tests",
        )
        parser.add_argument(
            "--ruff-version",
            choices=RUFF_VERSIONS,
            default=VERSIONS["ruff"],
            help="approved installed Ruff version, shared by both snapshots",
        )
        parser.add_argument("--signals", default="size,complexity,duplication")
        parser.add_argument("--min-lines", type=int, default=15)
        parser.add_argument("--min-tokens", type=int, default=100)
        parser.add_argument("--output", help="JSON output path (default stdout)")
        parser.add_argument("--report", help="optional Markdown output path")
        parser.set_defaults(func=run)
