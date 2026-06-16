"""Model-agnostic tool manifest and registry for Live sessions.

``LiveToolRegistry`` reads the SyntH action registry (``get_action_plugin_instructions``)
and produces ``ToolManifest`` objects.  Format-specific adapters in
``core/live_tool_adapters/`` convert manifests into wire-format declarations for
each engine family (Gemini, OpenAI Realtime, …).

Usage::

    from core.live_tool_registry import LiveToolRegistry
    from core.live_tool_adapters.gemini import GeminiToolAdapter

    manifests = LiveToolRegistry.build_manifests()
    tool_declarations = GeminiToolAdapter.to_declarations(manifests)
    # pass tool_declarations to LiveSessionManager.start_session(tools=...)
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.logging_utils import log_info, log_warning

logger = logging.getLogger(__name__)

_LOWER_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}

_UPPER_TYPE_MAP: dict[str, str] = {
    key: value.upper() for key, value in _LOWER_TYPE_MAP.items()
}

_SCHEMA_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "description",
    "enum",
    "format",
    "default",
    "nullable",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
)


def _convert_json_schema_types(
    schema: dict[str, Any],
    *,
    type_map: dict[str, str],
) -> dict[str, Any]:
    """Recursively convert JSON-schema type names for a target wire format."""

    result: dict[str, Any] = {}

    schema_type = schema.get("type")
    normalized_schema_type = (
        schema_type.lower() if isinstance(schema_type, str) else None
    )
    if isinstance(schema_type, str):
        result["type"] = (
            type_map[normalized_schema_type]
            if normalized_schema_type in type_map
            else schema_type
        )

    for key in _SCHEMA_PASSTHROUGH_KEYS:
        if key in schema:
            result[key] = deepcopy(schema[key])

    items = schema.get("items")
    if isinstance(items, dict):
        result["items"] = _convert_json_schema_types(items, type_map=type_map)
    elif normalized_schema_type == "array":
        # Gemini rejects ARRAY parameters without an items schema.
        result["items"] = {"type": type_map["string"]}

    properties = schema.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            str(name): _convert_json_schema_types(field, type_map=type_map)
            for name, field in properties.items()
            if isinstance(field, dict)
        }
    elif normalized_schema_type == "object":
        result["properties"] = {}

    required = schema.get("required")
    if isinstance(required, list):
        result["required"] = [str(name) for name in required if isinstance(name, str)]

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        converted_any_of = [
            _convert_json_schema_types(option, type_map=type_map)
            for option in any_of
            if isinstance(option, dict)
        ]
        if converted_any_of:
            result["anyOf"] = converted_any_of

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        converted_one_of = [
            _convert_json_schema_types(option, type_map=type_map)
            for option in one_of
            if isinstance(option, dict)
        ]
        if converted_one_of:
            result["oneOf"] = converted_one_of

    return result


def tool_parameter_schema(
    parameter: "ToolParameter",
    *,
    uppercase_types: bool = False,
) -> dict[str, Any]:
    """Render one ToolParameter back into JSON schema for a target API."""

    parameter_schema = getattr(parameter, "schema", None)
    base_schema = parameter_schema or {
        "type": parameter.type,
        "description": parameter.description,
        "enum": parameter.enum,
    }
    return _convert_json_schema_types(
        base_schema,
        type_map=_UPPER_TYPE_MAP if uppercase_types else _LOWER_TYPE_MAP,
    )


def _tool_parameter_from_field(
    field_name: str,
    field_meta: dict[str, Any],
    *,
    required: bool,
) -> ToolParameter:
    """Convert one normalized action field into a ToolParameter."""

    raw_type = str(field_meta.get("type", "string")).lower()
    return ToolParameter(
        name=field_name,
        type=raw_type,
        description=str(field_meta.get("description", "")),
        required=required,
        enum=field_meta.get("enum") or None,
        schema=deepcopy(field_meta),
    )


def _parameters_from_action_definition(
    action_name: str,
    action_def: dict[str, Any],
) -> list[ToolParameter]:
    """Extract ToolParameter entries from either payload- or schema-based actions."""

    from core.action_schema_converter import normalize_action_schema

    try:
        normalized = normalize_action_schema(action_name, action_def)
    except Exception:
        normalized = action_def

    payload_spec = normalized.get("payload", {})
    if isinstance(payload_spec, dict) and payload_spec:
        parameters: list[ToolParameter] = []
        for field_name, field_meta in payload_spec.items():
            if not isinstance(field_meta, dict):
                continue
            parameters.append(
                _tool_parameter_from_field(
                    str(field_name),
                    field_meta,
                    required=not bool(field_meta.get("optional", False)),
                )
            )
        return parameters

    schema = normalized.get("schema", {})
    if not isinstance(schema, dict):
        return []

    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not properties:
        return []

    required_fields = {
        str(name) for name in (schema.get("required") or []) if isinstance(name, str)
    }

    parameters = []
    for field_name, field_meta in properties.items():
        if not isinstance(field_meta, dict):
            continue
        parameters.append(
            _tool_parameter_from_field(
                str(field_name),
                field_meta,
                required=str(field_name) in required_fields,
            )
        )
    return parameters


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ToolParameter:
    """A single parameter in a tool's schema."""

    name: str
    type: str  # "string" | "integer" | "number" | "boolean" | "object" | "array"
    description: str = ""
    required: bool = True
    enum: list[str] | None = None
    schema: dict[str, Any] | None = None


@dataclass
class ToolManifest:
    """Model-agnostic description of a callable Live tool.

    Attributes:
        name:        SyntH action type — echoed back by the model as the
                     function name in a tool call.
        description: Natural-language explanation for the LLM.
        parameters:  Ordered list of accepted parameters.
        async_ok:    When True, engines that support async function calling
                     (e.g. Gemini 2.5) may run this tool non-blocking.
    """

    name: str
    description: str
    parameters: list[ToolParameter] = field(default_factory=list)
    async_ok: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class LiveToolRegistry:
    """Discovers callable tools from the SyntH action registry."""

    @staticmethod
    def build_manifests(
        interface_name: str | None = None,
        allowed_action_types: set[str] | None = None,
    ) -> list[ToolManifest]:
        """Build ``ToolManifest`` objects from all registered plugin actions.

        Queries ``get_action_plugin_instructions()`` (which aggregates
        ``get_supported_actions()`` across all loaded plugins and interfaces)
        and converts each action into a ``ToolManifest``.

        Args:
            interface_name: Optional name of the active interface.  Reserved for
                future per-interface tool filtering; currently stored as context
                only and does not affect the returned manifests.
            allowed_action_types: Optional set of action type names to include.
                When provided, only actions whose names are in this set are
                returned.  Pass ``None`` to include all registered actions.

        Returns:
            A list of manifests; empty if the action registry is unavailable.
        """
        try:
            from core.action_parser import get_action_plugin_instructions

            instructions = get_action_plugin_instructions()
        except Exception as exc:
            log_warning(
                f"[live_tool_registry] Could not load action instructions: {exc}"
            )
            return []

        if not instructions:
            return []

        manifests: list[ToolManifest] = []
        for action_name, instr in instructions.items():
            if not isinstance(instr, dict):
                continue

            # Filter to the allowed action types when specified
            if (
                allowed_action_types is not None
                and action_name not in allowed_action_types
            ):
                continue

            description: str = instr.get("description", f"Execute {action_name} action")
            parameters = _parameters_from_action_definition(action_name, instr)

            manifests.append(
                ToolManifest(
                    name=action_name,
                    description=description,
                    parameters=parameters,
                    async_ok=bool(instr.get("async_ok", False)),
                )
            )

        log_info(
            f"[live_tool_registry] Built {len(manifests)} tool manifests: "
            f"{[m.name for m in manifests]}"
        )
        return manifests

    @staticmethod
    def build_manifests_from_actions(
        actions: dict[str, Any],
    ) -> list[ToolManifest]:
        """Build ``ToolManifest`` objects from an already-computed actions dict.

        This variant accepts the ``available_actions`` dict that
        ``core.core_initializer.core_initializer.actions_block`` (or prompt_engine)
        already has — avoiding a second call to ``get_action_plugin_instructions()``.

        Args:
            actions: Mapping from action name to its schema / description dict
                (the same shape as ``available_actions`` from ``actions_block``).

        Returns:
            One ``ToolManifest`` per action entry in *actions*.
        """
        from core.action_schema_converter import normalize_action_schema

        manifests: list[ToolManifest] = []
        for action_name, action_def in actions.items():
            if not isinstance(action_def, dict):
                continue

            try:
                normalized = normalize_action_schema(action_name, action_def)
            except Exception:
                normalized = action_def

            description: str = str(
                normalized.get(
                    "description",
                    normalized.get("brief", f"Execute {action_name} action"),
                )
            )
            parameters = _parameters_from_action_definition(action_name, normalized)

            manifests.append(
                ToolManifest(
                    name=action_name,
                    description=description,
                    parameters=parameters,
                    async_ok=bool(action_def.get("async_ok", False)),
                )
            )

        return manifests
