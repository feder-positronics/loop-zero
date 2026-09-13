"""In-process check-policy classification.

This module intentionally mirrors ``core/tools/checks.py`` so the installed
package can report policy without importing or executing candidate snapshot
code.
"""

GROUPS = ("required", "advisory", "scheduled")
INFRASTRUCTURE = {"runner_loss", "cancelled_concurrency", "network_timeout"}
CONCLUSIONS = {"success", "failure", "pending", *INFRASTRUCTURE}


def policy_checks(config):
    """Yield (group, name) pairs; names must be nonempty, unique and disjoint."""
    if not isinstance(config, dict):
        raise ValueError("checks must be a table")
    names = set()
    for group in GROUPS:
        checks = config.get(group)
        if not isinstance(checks, list):
            raise ValueError(f"checks.{group} must be a list of check names")
        for name in checks:
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError("check names must be nonempty, unique, and disjoint")
            names.add(name)
            yield group, name


def classify_one(group, name, result):
    """Class 3 describes an execution failure, not a permanent check category."""
    if not isinstance(result, dict):
        raise ValueError(f"{name}: result must be an object")
    conclusion = result.get("conclusion")
    attempts = result.get("attempts", 1)
    if conclusion not in CONCLUSIONS or type(attempts) is not int or attempts < 1:
        raise ValueError(f"{name}: invalid conclusion or attempts")
    blocks = group == "required" and conclusion != "success"
    if conclusion in INFRASTRUCTURE:
        check_class = 3
        action = (
            "retry once automatically"
            if attempts == 1
            else "report infrastructure; no code verdict"
        )
        if group == "required":
            action += "; required evidence unavailable; would block PR"
    else:
        check_class = 2 if group == "scheduled" else 1
        action = "would block PR" if blocks else "does not block PR"
        if blocks and conclusion == "pending":
            action += "; no result yet"
    return {
        "name": name,
        "group": group,
        "class": check_class,
        "conclusion": conclusion,
        "blocks": blocks,
        "action": action,
    }


def classify(config, results):
    """Report every policy check; ``blocks`` marks required checks without success."""
    if not isinstance(results, dict):
        raise ValueError("results must be an object keyed by check name")
    rows = [
        classify_one(group, name, results.get(name, {"conclusion": "pending"}))
        for group, name in policy_checks(config)
    ]
    if results.keys() - {row["name"] for row in rows}:
        raise ValueError("results contain checks absent from the policy")
    return rows
