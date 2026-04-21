"""OpenAI Realtime format adapter: ``ToolManifest`` → OpenAI tool declarations.

Stub — not yet connected to a live engine.  Implements the wire format
expected by the OpenAI Realtime API ``session.update`` ``tools`` field:

.. code-block:: json

    [
        {
            "type": "function",
            "name": "my_tool",
            "description": "...",
            "parameters": {
                "type": "object",
                "properties": { ... },
                "required": [ ... ]
            }
        }
    ]

OpenAI Realtime uses standard JSON Schema (lowercase type strings), unlike
the Gemini adapter which needs uppercase strings.
"""

from __future__ import annotations

from typing import Any

from core.logging_utils import log_info
from core.live_tool_registry import ToolManifest


class OpenAIRealtimeToolAdapter:
    """Converts ``ToolManifest`` objects to OpenAI Realtime API tool dicts."""

    @staticmethod
    def to_declarations(manifests: list[ToolManifest]) -> list[dict[str, Any]]:
        """Convert manifests to OpenAI Realtime ``tools`` list.

        Args:
            manifests: Manifests from ``LiveToolRegistry.build_manifests()``.

        Returns:
            A list of tool dicts in the OpenAI Realtime wire format.
        """
        tools: list[dict[str, Any]] = []

        for manifest in manifests:
            properties: dict[str, Any] = {}
            required_fields: list[str] = []

            for param in manifest.parameters:
                prop: dict[str, Any] = {"type": param.type}
                if param.description:
                    prop["description"] = param.description
                if param.enum:
                    prop["enum"] = param.enum
                properties[param.name] = prop
                if param.required:
                    required_fields.append(param.name)

            params: dict[str, Any] = {
                "type": "object",
                "properties": properties,
            }
            if required_fields:
                params["required"] = required_fields

            tool: dict[str, Any] = {
                "type": "function",
                "name": manifest.name,
                "description": manifest.description,
                "parameters": params,
            }

            tools.append(tool)

        log_info(
            f"[openai_realtime_tool_adapter] Built {len(tools)} declarations: "
            f"{[t['name'] for t in tools]}"
        )
        return tools
