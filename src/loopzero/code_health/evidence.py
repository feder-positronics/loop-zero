"""Pure validation for consumer-owned status registration and PR evidence."""

from .analyzers import PYTHON_TARGET, VERSIONS
from .source import digest


def collector_status(report, *, exit_code, head, policy, base=None):
    """Validate identity/completion before a consumer records a successful run.

    This checks consistency, not authenticity: the caller must own execution,
    approved policy, expected SHAs, and artifact transport.
    """
    policy = {"python_target": PYTHON_TARGET, **policy}
    errors = []
    if type(exit_code) is not int or exit_code != 0:
        errors.append("collector exited unsuccessfully")
    if not isinstance(report, dict):
        report = {}
    if (
        type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("valid") is not True
    ):
        errors.append("missing, unsupported, or incomplete artifact")
    if report.get("mode") != ("compare" if base else "snapshot"):
        errors.append("wrong collection mode")
    if report.get("policy") != policy or report.get("policy_id") != digest(policy):
        errors.append("policy mismatch")
    expected_tools = {}
    for tool, signal in (("ruff", "complexity"), ("jscpd", "duplication")):
        if signal in policy["signals"]:
            expected_tools[tool] = (
                policy.get("ruff_version", VERSIONS[tool])
                if tool == "ruff"
                else VERSIONS[tool]
            )
    if report.get("tools") != expected_tools:
        errors.append("tool policy mismatch")
    for name, sha in (("head", head), ("base", base)):
        if sha is None:
            if name in report:
                errors.append(f"unexpected {name} snapshot")
            continue
        snapshot = report.get(name)
        if not isinstance(snapshot, dict) or snapshot.get("sha") != sha:
            errors.append(f"{name} source mismatch")
            continue
        requested, completed = snapshot.get("requested"), snapshot.get("completed")
        if (
            not isinstance(requested, list)
            or not isinstance(completed, list)
            or not all(isinstance(value, str) for value in requested + completed)
            or sorted(requested) != sorted(policy["signals"])
            or sorted(completed) != sorted(policy["signals"])
            or snapshot.get("errors") != []
        ):
            errors.append(f"{name} requested coverage incomplete")
    if report.get("threshold_failed") is not False:
        errors.append("unexpected threshold verdict")
    return {
        "valid": not errors,
        "exit_code": exit_code,
        "threshold_failed": False,
        "git_head": head,
        "reason": "; ".join(errors) if errors else "source-bound artifact captured",
    }
