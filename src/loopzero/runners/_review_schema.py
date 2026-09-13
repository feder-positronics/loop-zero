"""Unchanged schema-only dependencies of the governed result contract."""

from collections.abc import Mapping, Sequence

def required_review_sections(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return the closed terminal-review section set for canonical paths."""
    return ("code", "security") if paths else ("code",)

class ReviewChainError(ValueError):
    """Raised when review-chain evidence is incomplete or inconsistent."""


def validate_review_chain_task(task: Mapping[str, object]) -> tuple[str, ...]:
    """Validate the hash-bound closed section set selected by the outer gate."""
    raw_paths = task.get("security_trigger_paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path for path in raw_paths
    ):
        raise ReviewChainError("security_trigger_paths must be a string list")
    if raw_paths != sorted(set(raw_paths)):
        raise ReviewChainError("security_trigger_paths must be sorted and unique")
    expected = required_review_sections(tuple(raw_paths))
    raw_sections = task.get("required_sections")
    if not isinstance(raw_sections, list) or tuple(raw_sections) != expected:
        raise ReviewChainError(
            "required_sections must exactly match the security trigger classifier"
        )
    return expected


def review_sections_schema(
    required_sections: Sequence[str],
    *,
    finding_schema: Mapping[str, object],
    bounded_string: Mapping[str, object],
) -> dict[str, object]:
    """Build the strict model-output fragment for one primary review chain."""
    properties: dict[str, object] = {}
    for section in required_sections:
        section_properties: dict[str, object] = {
            "completion": {"type": "string", "const": "completed"},
            "verdict": {"type": "string", "enum": ["clean", "findings"]},
            "findings": {
                "type": "array",
                "maxItems": 64,
                "items": dict(finding_schema),
            },
        }
        required = ["completion", "verdict", "findings"]
        if section == "security":
            section_properties["threat_model_summary"] = dict(bounded_string)
            required.append("threat_model_summary")
        properties[section] = {
            "type": "object",
            "properties": section_properties,
            "required": required,
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(required_sections),
        "additionalProperties": False,
    }
