"""Pure review-section policy and schema shared by admission and runtime callers.

Configured sections must come from approved consumer policy, independently of
worker task contents. Security triggers always add the mandatory security section.
"""

from collections.abc import Mapping, Sequence


def required_review_sections(
    paths: Sequence[str],
    *,
    configured_sections: Sequence[str] = ("code", "security"),
    require_security: bool = False,
) -> tuple[str, ...]:
    """Select sections, retaining the conservative unconfigured legacy mode.

    ``require_security`` can only add coverage; a trigger always requires it.
    """
    sections = tuple(
        dict.fromkeys(
            section for section in configured_sections if section != "security"
        )
    )
    if not configured_sections:
        raise ReviewChainError("review chain requires configured required sections")
    return (*sections, "security") if paths or require_security else sections


class ReviewChainError(ValueError):
    """Raised when review-chain evidence is incomplete or inconsistent."""


def validate_review_chain_task(
    task: Mapping[str, object],
    *,
    configured_sections: Sequence[str] = ("code", "security"),
    require_security: bool = False,
) -> tuple[str, ...]:
    """Validate the hash-bound closed section set selected by the outer gate."""
    raw_paths = task.get("security_trigger_paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path for path in raw_paths
    ):
        raise ReviewChainError("security_trigger_paths must be a string list")
    if raw_paths != sorted(set(raw_paths)):
        raise ReviewChainError("security_trigger_paths must be sorted and unique")
    expected = required_review_sections(
        raw_paths,
        configured_sections=configured_sections,
        require_security=require_security,
    )
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
