"""Snapshot collection and conservative comparison of structural observations."""

import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from . import analyzers
from .python_metrics import functions
from .source import digest, read_source


def snapshot(root, ref, policy):
    sha, files, excluded, errors = read_source(root, ref, policy)
    inventory = []
    for path, file in files.items():
        if path.endswith(".py"):
            try:
                inventory.extend(functions(path, file["text"]))
            except (SyntaxError, ValueError, RecursionError) as exc:
                errors.append(f"Python parse failed: {path}: {exc}")
    result = {
        "sha": sha,
        "files": {
            p: {k: v for k, v in f.items() if k != "text"} for p, f in files.items()
        },
        "functions": inventory,
        "excluded": excluded,
        "errors": errors,
        "completed": [] if errors else ["size"],
        "requested": policy["signals"],
        "duplication": {},
    }
    with tempfile.TemporaryDirectory(prefix="loopzero-health-") as temporary:
        work = Path(temporary)
        for path, file in files.items():
            destination = work / "source" / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(file["text"], encoding="utf-8")
        for signal in policy["signals"]:
            if signal == "size" or not files:
                continue
            try:
                if signal == "complexity":
                    gaps = analyzers.complexity(
                        work, inventory, policy.get("ruff_version")
                    )
                    if gaps:
                        errors.extend(gaps)
                        continue
                else:
                    # Separate corpora prevent test copies from inflating production evidence.
                    for category in ("production", "tests"):
                        selected = {
                            p: f for p, f in files.items() if f["category"] == category
                        }
                        if not selected:
                            continue
                        with tempfile.TemporaryDirectory(
                            prefix="loopzero-clones-"
                        ) as clone_dir:
                            clone_work = Path(clone_dir)
                            for path, file in selected.items():
                                destination = clone_work / "source" / path
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                destination.write_text(file["text"], encoding="utf-8")
                            data = analyzers.duplication(clone_work, selected, policy)
                            result["duplication"][category] = data
                            if data["invalid"]:
                                raise ValueError(f"{category}: invalid detector ranges")
                result["completed"].append(signal)
            except (
                ValueError,
                OSError,
                KeyError,
                TypeError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                errors.append(f"{signal}: {exc}")
    result["totals"] = {
        category: {
            "files": sum(f["category"] == category for f in files.values()),
            "lines": sum(
                f["lines"] for f in files.values() if f["category"] == category
            ),
        }
        for category in ("production", "tests")
    }
    result["coverage"] = {
        "physical_files": len(files),
        "python_functions": len(inventory),
        "complexity_measured": sum(f["complexity"] is not None for f in inventory),
        "complexity_unsupported": [p for p in files if not p.endswith(".py")],
    }
    return result


def identity(function):
    return function["path"], function["name"]


def compare(base, head):
    counts = Counter(identity(f) for f in base["functions"])
    head_counts = Counter(identity(f) for f in head["functions"])
    previous = {identity(f): f for f in base["functions"] if counts[identity(f)] == 1}
    changes, unmatched = [], []
    for function in head["functions"]:
        old = previous.get(identity(function))
        if old is None or head_counts[identity(function)] != 1:
            unmatched.append(function)
            continue
        for metric in ("span", "complexity"):
            before, after = old[metric], function[metric]
            if before is not None and after is not None and before != after:
                changes.append(
                    {
                        "kind": metric,
                        "path": function["path"],
                        "name": function["name"],
                        "line": function["line"],
                        "before": before,
                        "after": after,
                        "delta": after - before,
                    }
                )
    clone_leads = []
    for category, data in head["duplication"].items():
        if not all("duplication" in snapshot["completed"] for snapshot in (base, head)):
            continue
        old = base["duplication"].get(category, {"pairs": []})
        old_fragments = {
            (x["path"], x["fingerprint"])
            for pair in old["pairs"]
            for x in pair["locations"]
        }

        def relationship(pair):
            return tuple(
                sorted((x["path"], x["fingerprint"]) for x in pair["locations"])
            )

        previous_pairs = Counter(relationship(pair) for pair in old["pairs"])
        introduced = (
            Counter(relationship(pair) for pair in data["pairs"]) - previous_pairs
        )
        new_locations = set()
        for pair in data["pairs"]:
            key = relationship(pair)
            if introduced[key] > 0:
                new_locations.update(
                    (x["path"], x["line"], x["end"]) for x in pair["locations"]
                )
                introduced[key] -= 1
        for family in data["families"]:
            if any((x["path"], x["line"], x["end"]) in new_locations for x in family):
                clone_leads.append(
                    {
                        "kind": "duplication-candidate",
                        "category": category,
                        "locations": [
                            {
                                **x,
                                "fragment_seen_in_base": fragment_seen(
                                    x, base, head, old_fragments
                                ),
                            }
                            for x in family
                        ],
                        "matching": "new-or-changed-fragments",
                    }
                )
    growth = {
        category: {
            key: head["totals"][category][key] - base["totals"][category][key]
            for key in ("files", "lines")
        }
        for category in ("production", "tests")
    }
    file_changes = [
        {
            "path": path,
            "before": base["files"].get(path, {}).get("lines", 0),
            "after": head["files"].get(path, {}).get("lines", 0),
            "state": "added"
            if path not in base["files"]
            else "removed"
            if path not in head["files"]
            else "modified",
        }
        for path in sorted(base["files"].keys() | head["files"].keys())
        if base["files"].get(path) != head["files"].get(path)
    ]
    return {
        "unavailable_signals": sorted(
            set(base.get("requested", []) + head.get("requested", []))
            - (set(base["completed"]) & set(head["completed"]))
        ),
        "changes": changes,
        "file_changes": file_changes,
        "unmatched_head_functions": unmatched,
        "unmatched_base_functions": [
            f
            for f in base["functions"]
            if identity(f) not in head_counts
            or counts[identity(f)] != 1
            or head_counts[identity(f)] != 1
        ],
        "growth": growth,
        "clone_candidates": clone_leads,
        "highlights": highlights(clone_leads, changes),
    }


def highlights(clones, changes):
    growing = sorted([c for c in changes if c["delta"] > 0], key=lambda c: -c["delta"])
    selected = clones[:1]
    for kind in ("complexity", "span"):
        selected.extend([c for c in growing if c["kind"] == kind][:1])
    for candidate in clones + growing:
        if len(selected) == 3:
            break
        if candidate not in selected:
            selected.append(candidate)
    return selected


def fragment_seen(location, base, head, old_fragments):
    path = location["path"]
    if path not in base["files"]:
        return False
    if base["files"][path]["blob"] == head["files"][path]["blob"]:
        return True
    if (path, location["fingerprint"]) in old_fragments:
        return True
    return None


def collect(root, head, policy, base=None):
    policy = {"python_target": analyzers.PYTHON_TARGET, **policy}
    if policy["python_target"] != analyzers.PYTHON_TARGET:
        raise ValueError(f"unsupported Python target: {policy['python_target']}")
    result = {
        "schema_version": 1,
        "mode": "compare" if base else "snapshot",
        "policy": policy,
        "policy_id": digest(policy),
        "tools": {
            k: policy.get("ruff_version", v) if k == "ruff" else v
            for k, v in analyzers.VERSIONS.items()
            if {"ruff": "complexity", "jscpd": "duplication"}[k] in policy["signals"]
        },
    }
    if base:
        result["base"] = snapshot(root, base, policy)
    result["head"] = snapshot(root, head, policy)
    if base:
        result["comparison"] = compare(result["base"], result["head"])
    result["valid"] = not any(
        result[key]["errors"] for key in ("base", "head") if key in result
    )
    result["threshold_failed"] = False
    return result
