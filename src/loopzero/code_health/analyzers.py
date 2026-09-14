"""Pinned subprocess adapters; candidate files are data, never configuration."""

import ast
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from .source import digest, environment, physical_lines

VERSIONS = {"ruff": "0.16.7", "jscpd": "5.2.0"}
RUFF_VERSIONS = ("0.5.7", "0.16.7")
PYTHON_TARGET = "py313"


def tool(name, work, arguments, accepted=(0,), expected_version=None):
    if name not in VERSIONS:
        raise ValueError(f"unsupported analyzer: {name}")
    expected_version = expected_version or VERSIONS[name]
    supported = RUFF_VERSIONS if name == "ruff" else (VERSIONS[name],)
    if expected_version not in supported:
        raise ValueError(f"unsupported {name} version: {expected_version}")
    sibling = Path(sys.executable).parent / name
    executable = str(sibling) if sibling.is_file() else shutil.which(name)
    if not executable:
        remedy = (
            "install loopzero[code-health-duplication]"
            if name == "jscpd"
            else f"install the approved Ruff {expected_version}"
        )
        raise ValueError(f"{name} unavailable; {remedy}")
    env = environment() | {"HOME": str(work), "XDG_CONFIG_HOME": str(work)}

    def run(args):
        return subprocess.run(
            [executable, *args],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )

    version = run(["--version"])
    if version.returncode or not re.search(
        r"(?<![\d.])" + re.escape(expected_version) + r"(?![\d.])", version.stdout
    ):
        raise ValueError(
            f"{name} version mismatch: expected {expected_version}, got {version.stdout.strip()}; {version.stderr.strip()}"
        )
    result = run(arguments)
    if result.returncode not in accepted:
        raise ValueError(f"{name} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def complexity(work, inventory, ruff_version=None):
    corpus = work / "python"
    corpus.mkdir()
    for path in sorted({f["path"] for f in inventory}):
        destination = corpus / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / "source" / path, destination)
    raw = tool(
        "ruff",
        work,
        [
            "check",
            "python",
            "--isolated",
            "--target-version",
            PYTHON_TARGET,
            "--no-respect-gitignore",
            "--exclude",
            "",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=0",
            "--ignore-noqa",
            "--no-cache",
            "--output-format",
            "json",
        ],
        (0, 1),
        expected_version=ruff_version,
    )
    values, errors, failed_paths = {}, [], set()
    for diagnostic in json.loads(raw):
        path = Path(diagnostic["filename"]).relative_to(corpus).as_posix()
        match = re.search(r"\((\d+) > 0\)", diagnostic["message"])
        if diagnostic["code"] != "C901" or not match:
            failed_paths.add(path)
            errors.append(f"Ruff could not measure {path}: {diagnostic['message']}")
            continue
        values[(path, diagnostic["location"]["row"])] = int(match[1])
    for function in inventory:
        value = values.pop((function["path"], function["line"]), None)
        function["complexity"] = None if function["path"] in failed_paths else value
    missing = [
        f"{f['path']}:{f['line']} ({f['name']})"
        for f in inventory
        if f["complexity"] is None and f["path"] not in failed_paths
    ]
    if missing:
        errors.append(
            f"Ruff left {len(missing)} functions unmeasured: {', '.join(missing[:5])}"
        )
    if values:
        errors.append(f"unmatched Ruff diagnostics: {sorted(values)[:5]}")
    return errors


def location(raw, files, source):
    path = Path(raw["name"])
    if path.is_absolute():
        path = path.relative_to(source)
    elif path.parts and path.parts[0] == "source":
        path = Path(*path.parts[1:])
    name = path.as_posix()
    start, end = raw["start"], raw["end"]
    if (
        name not in files
        or type(start) is not int
        or type(end) is not int
        or not 1 <= start <= end <= files[name]["lines"]
    ):
        raise ValueError(f"invalid clone range: {raw}")
    fragment = "\n".join(physical_lines(files[name]["text"])[start - 1 : end])
    return {
        "path": name,
        "line": start,
        "end": end,
        "fingerprint": digest(fragment),
    }, fragment


def classify(fragment, path):
    if not path.endswith(".py"):
        return "unclassified"
    try:
        nodes = ast.parse(textwrap.dedent(fragment)).body
    except (SyntaxError, ValueError):
        return "unclassified"
    if nodes and all(isinstance(n, (ast.Import, ast.ImportFrom)) for n in nodes):
        return "imports"
    if nodes and all(
        isinstance(n, (ast.Assign, ast.AnnAssign))
        and isinstance(n.value, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Constant))
        for n in nodes
    ):
        return "policy-data-candidate"
    if nodes and all(
        isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for n in nodes
    ):
        return "documentation"
    return "behavior-candidate"


def validate_clones(raw, files, source):
    pairs, errors = [], []
    for candidate in raw["duplicates"]:
        try:
            first, a = location(candidate["firstFile"], files, source)
            second, b = location(candidate["secondFile"], files, source)
            pairs.append(
                {
                    "locations": sorted(
                        [first, second], key=lambda x: (x["path"], x["line"])
                    ),
                    "reported_kind": candidate.get("kind", "unspecified"),
                    "whole_lines_equal": a == b,
                    "classification": classify(a, first["path"]),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"error": str(exc), "raw": candidate})
    pairs.sort(
        key=lambda pair: [(x["path"], x["line"], x["end"]) for x in pair["locations"]]
    )
    return pairs, errors


def families(pairs):
    groups = []
    for pair in pairs:
        locations = {(x["path"], x["line"], x["end"]): x for x in pair["locations"]}
        merged, remaining = [], []
        for group in groups:
            overlap = any(
                a[0] == b[0] and a[1] <= b[2] and b[1] <= a[2]
                for a in locations
                for b in group
            )
            (merged if overlap else remaining).append(group)
        for group in merged:
            locations.update(group)
        groups = remaining + [locations]
    return sorted(
        [
            sorted(group.values(), key=lambda x: (x["path"], x["line"]))
            for group in groups
        ],
        key=lambda group: [(x["path"], x["line"], x["end"]) for x in group],
    )


def duplication(work, files, policy):
    (work / "detector.json").write_text("{}", encoding="utf-8")
    tool(
        "jscpd",
        work,
        [
            "source",
            "--config",
            str(work / "detector.json"),
            "--min-lines",
            str(policy["min_lines"]),
            "--min-tokens",
            str(policy["min_tokens"]),
            "--mode",
            "weak",
            "--reporters",
            "json",
            "--output",
            str(work / "clones"),
            "--silent",
            "--no-gitignore",
            "--max-size",
            "5mb",
            "--workers",
            "2",
        ],
    )
    raw = json.loads((work / "clones/jscpd-report.json").read_text(encoding="utf-8"))
    pairs, errors = validate_clones(raw, files, work / "source")
    statistics = {
        key: value
        for key, value in raw.get("statistics", {}).items()
        if key != "detectionDate"
    }
    affected = {path: set() for path in files}
    for pair in pairs:
        for loc in pair["locations"]:
            affected[loc["path"]].update(range(loc["line"], loc["end"] + 1))
    return {
        "pairs": pairs,
        "families": families(pairs),
        "invalid": errors,
        "physical_input_files": len(files),
        "unique_affected_lines": sum(map(len, affected.values())),
        "detector_statistics": statistics,
    }
