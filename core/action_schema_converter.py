# core/action_schema_converter.py
"""Convert between legacy and new action schema formats."""

from typing import Dict, Any
from core.logging_utils import log_debug


def normalize_action_schema(
    action_name: str, action_def: Dict[str, Any]
) -> Dict[str, Any]:
    """Convert old action schema format to new normalized format.

    Old format:
    {
        "description": "...",
        "required_fields": ["..."],
        "optional_fields": ["..."],
        "instructions": {...}
    }

    New format:
    {
        "schema": {
            "type": "object",
            "properties": {...},
            "required": [...]
        },
        "brief": "...",
        "examples": {
            "description": "...",
            "instructions": {...},
            "examples": [...]
        }
    }

    Parameters
    ----------
    action_name : str
        Name of the action (for logging)
    action_def : Dict[str, Any]
        Action definition in either old or new format

    Returns
    -------
    Dict[str, Any]
        Normalized action definition in new format
    """

    # Check if already in new format
    if "schema" in action_def and "brief" in action_def:
        log_debug(
            f"[action_schema_converter] Action '{action_name}' already in new format"
        )
        return action_def

    # Convert from old format
    if "description" in action_def or "required_fields" in action_def:
        log_debug(
            f"[action_schema_converter] Converting action '{action_name}' from old format to new"
        )

        description = action_def.get("description", "")
        required_fields = action_def.get("required_fields", [])
        optional_fields = action_def.get("optional_fields", [])
        instructions = action_def.get("instructions", {})
        examples = (
            action_def.get("examples", [])
            if isinstance(action_def.get("examples"), list)
            else []
        )

        # Build schema from required_fields and optional_fields
        properties = {}
        for field in required_fields + optional_fields:
            properties[field] = {
                "type": "string",  # Default type, will be overridden by plugin-specific schemas
                "description": f"Field: {field}",
            }

        # Create new format
        normalized = {
            "schema": {
                "type": "object",
                "properties": properties,
                "required": required_fields,
            },
            "brief": description,  # Use description as brief
            "examples": {
                "description": description,
                "instructions": instructions,
                "examples": examples,
            },
            "source": action_def.get("source", "unknown"),
            # Propagate optional security metadata if present in legacy definition
            "security_level": action_def.get("security_level"),
            "external_effects": action_def.get("external_effects"),
        }

        return normalized

    # Unknown format - return as-is
    log_debug(
        f"[action_schema_converter] Action '{action_name}' format unrecognized, passing through"
    )
    return action_def


def extract_for_llm_prompt(
    action_name: str, action_def: Dict[str, Any]
) -> Dict[str, Any]:
    """Extract only the parts needed for LLM prompt (schema + brief).

    This minimizes token usage by sending only essential information to the LLM.
    Detailed examples and instructions are kept for corrector use.

    Parameters
    ----------
    action_name : str
        Name of the action
    action_def : Dict[str, Any]
        Normalized action definition

    Returns
    -------
    Dict[str, Any]
        Action definition with only schema and brief
    """
    normalized = normalize_action_schema(action_name, action_def)

    result = {
        "schema": normalized.get("schema", {}),
        "brief": normalized.get("brief", ""),
        "source": normalized.get("source", "unknown"),
    }

    return result


def extract_for_corrector(
    action_name: str, action_def: Dict[str, Any]
) -> Dict[str, Any]:
    """Extract the complete action definition for corrector (schema + brief + examples).

    When the LLM makes mistakes, the corrector sends more detailed information
    including examples and instructions to help the LLM understand and correct itself.

    Parameters
    ----------
    action_name : str
        Name of the action
    action_def : Dict[str, Any]
        Normalized action definition

    Returns
    -------
    Dict[str, Any]
        Complete action definition with all details
    """
    normalized = normalize_action_schema(action_name, action_def)
    return normalized
