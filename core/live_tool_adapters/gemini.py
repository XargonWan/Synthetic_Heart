"""Gemini format adapter: ``ToolManifest`` → ``genai.types.Tool``.

Converts the model-agnostic ``ToolManifest`` list produced by
``LiveToolRegistry`` into the ``google.genai.types.FunctionDeclaration``
list required by the Gemini Live API ``tools`` session parameter.

The adapter mirrors the behaviour of the legacy
``discord_interface._build_gemini_tool_declarations()`` function but works
from manifests rather than from the action registry directly, making it
reusable across any code path that needs Gemini-formatted declarations.
"""

from __future__ import annotations

import logging
from typing import Any

from core.logging_utils import log_info, log_warning
from core.live_tool_registry import ToolManifest

logger = logging.getLogger(__name__)

# Gemini JSON-Schema uses uppercase type strings (STRING, INTEGER, …).
_TYPE_MAP: dict[str, str] = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}


class GeminiToolAdapter:
    """Converts ``ToolManifest`` objects to Gemini Live API declarations."""

    @staticmethod
    def to_declarations(
        manifests: list[ToolManifest],
        *,
        engine_supports_nonblocking: bool = False,
    ) -> list[Any] | None:
        """Convert manifests to a ``[genai.types.Tool(...)]`` list.

        Args:
            manifests: Manifests from ``LiveToolRegistry.build_manifests()``.
            engine_supports_nonblocking: Pass ``True`` for Gemini 2.5+ to
                emit ``behavior="NON_BLOCKING"`` on manifests with
                ``async_ok=True``.  Gemini 3.1 ignores / rejects this field,
                so leave as ``False`` (default) for that model.

        Returns:
            A list containing a single ``types.Tool``, or ``None`` if the
            google-genai SDK is unavailable or no declarations could be built.
        """
        try:
            from google.genai import types as genai_types
        except ImportError:
            log_warning(
                "[gemini_tool_adapter] google-genai SDK unavailable — "
                "skipping tool declarations"
            )
            return None

        if not manifests:
            return None

        declarations: list[Any] = []
        for manifest in manifests:
            properties: dict[str, Any] = {}
            required_fields: list[str] = []

            for param in manifest.parameters:
                prop: dict[str, Any] = {
                    "type": _TYPE_MAP.get(param.type, param.type.upper()),
                }
                if param.description:
                    prop["description"] = param.description
                if param.enum:
                    prop["enum"] = param.enum
                properties[param.name] = prop
                if param.required:
                    required_fields.append(param.name)

            schema: dict[str, Any] = {"type": "OBJECT", "properties": properties}
            if required_fields:
                schema["required"] = required_fields

            fd_kwargs: dict[str, Any] = {
                "name": manifest.name,
                "description": manifest.description,
                "parameters": schema,
            }
            if engine_supports_nonblocking and manifest.async_ok:
                fd_kwargs["behavior"] = "NON_BLOCKING"

            try:
                fd = genai_types.FunctionDeclaration(**fd_kwargs)
                declarations.append(fd)
            except Exception as exc:
                log_warning(
                    f"[gemini_tool_adapter] Failed to build declaration "
                    f"for {manifest.name!r}: {exc}"
                )

        if not declarations:
            return None

        log_info(
            f"[gemini_tool_adapter] Built {len(declarations)} Gemini declarations: "
            f"{[d.name for d in declarations]}"
        )
        return [genai_types.Tool(function_declarations=declarations)]
