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
from dataclasses import dataclass, field
from typing import Any

from core.logging_utils import log_info, log_warning

logger = logging.getLogger(__name__)


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
            payload_spec: dict = instr.get("payload", {})
            parameters: list[ToolParameter] = []

            for field_name, field_meta in payload_spec.items():
                if not isinstance(field_meta, dict):
                    continue
                raw_type = str(field_meta.get("type", "string")).lower()
                # Normalise: SyntH uses lowercase type strings; keep them that way
                param = ToolParameter(
                    name=field_name,
                    type=raw_type,
                    description=str(field_meta.get("description", "")),
                    required=not bool(field_meta.get("optional", False)),
                    enum=field_meta.get("enum") or None,
                )
                parameters.append(param)

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
                normalized = action_def  # type: ignore[assignment]

            description: str = str(
                normalized.get(
                    "description",
                    normalized.get("brief", f"Execute {action_name} action"),
                )
            )
            payload_spec: dict = normalized.get("payload", {}) or {}
            parameters: list[ToolParameter] = []

            for field_name, field_meta in payload_spec.items():
                if not isinstance(field_meta, dict):
                    continue
                raw_type = str(field_meta.get("type", "string")).lower()
                param = ToolParameter(
                    name=field_name,
                    type=raw_type,
                    description=str(field_meta.get("description", "")),
                    required=not bool(field_meta.get("optional", False)),
                    enum=field_meta.get("enum") or None,
                )
                parameters.append(param)

            manifests.append(
                ToolManifest(
                    name=action_name,
                    description=description,
                    parameters=parameters,
                    async_ok=bool(action_def.get("async_ok", False)),  # type: ignore[union-attr]
                )
            )

        return manifests
