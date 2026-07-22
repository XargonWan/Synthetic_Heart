"""Unified tool registry for Synth's agentic runtime.

This module is the bridge that makes the existing action registry **valid
tools** for an agentic LLM, and merges them with tools discovered from
external MCP servers (see ``core/mcp_bridge``).

Design
------
* Internal SyntH actions (from ``core_initializer.actions_block`` /
  ``LiveToolRegistry``) become ``ToolManifest`` objects with
  ``source="internal"``.
* External MCP tools (once the MCP client bridge is wired in) become
  ``ToolManifest`` objects with ``source="mcp:<server>"``.
* Every manifest carries ``security_level`` and ``external_effects`` so the
  existing :func:`core.action_safety.is_action_allowed_for_execution` gate can
  be reused unchanged for both kinds of tools.

The registry is intentionally read-mostly: it is rebuilt on demand (and on
``refresh_actions_block``) so newly registered plugins/interfaces are picked
up without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.logging_utils import log_debug, log_info
from core.live_tool_registry import ToolManifest, ToolParameter

# Source discriminators used by the agent executor / safety gate.
SOURCE_INTERNAL = "internal"
SOURCE_MCP = "mcp"


@dataclass
class UnifiedToolManifest(ToolManifest):
    """A ``ToolManifest`` enriched with agent-runtime metadata.

    Extends :class:`core.live_tool_registry.ToolManifest` so it remains
    compatible with the existing Live tool pipeline, while adding the fields
    the agent loop and safety gate need.
    """

    source: str = SOURCE_INTERNAL  # "internal" | "mcp:<server_name>"
    security_level: str = "low"  # "low" | "medium" | "high"
    external_effects: list[str] = field(default_factory=list)
    server_name: str | None = None  # MCP server name when source starts with "mcp:"

    def is_external(self) -> bool:
        """Return True when this tool comes from an external MCP server."""
        return self.source != SOURCE_INTERNAL

    def to_action_dict(self) -> dict[str, Any]:
        """Render the manifest as a SyntH action definition dict.

        Used by the agent executor to feed the tool back through the standard
        action-parse / safety / dispatch path.  Internal tools already exist in
        the action registry; external MCP tools are synthesized into the same
        shape so they flow through the identical gate.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.parameters:
            properties[param.name] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                properties[param.name]["enum"] = param.enum
            if param.required:
                required.append(param.name)

        return {
            "schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
            "brief": self.description,
            "security_level": self.security_level,
            "external_effects": self.external_effects,
            "source": self.source,
            "server_name": self.server_name,
        }


class ToolRegistry:
    """Aggregates internal actions and external MCP tools into one tool set."""

    def __init__(self) -> None:
        self._tools: dict[str, UnifiedToolManifest] = {}
        self._mcp_sources: dict[str, str] = {}  # tool_name -> server_name

    # -- internal actions --------------------------------------------------

    def load_internal_actions(
        self, actions: dict[str, Any] | None = None
    ) -> list[UnifiedToolManifest]:
        """Convert SyntH action definitions into unified tool manifests.

        Args:
            actions: Optional ``available_actions`` dict (from
                ``core_initializer.actions_block``).  When ``None``, the live
                action instructions are pulled via
                ``LiveToolRegistry.build_manifests``.

        Returns:
            The list of internal tool manifests that were (re)loaded.
        """
        from core.live_tool_registry import LiveToolRegistry

        if actions is None:
            base = LiveToolRegistry.build_manifests()
        else:
            base = LiveToolRegistry.build_manifests_from_actions(actions)

        # Replace the internal tool set atomically while preserving MCP tools.
        stale_internal = [
            name for name, tool in self._tools.items() if tool.source == SOURCE_INTERNAL
        ]
        for name in stale_internal:
            self._tools.pop(name, None)

        loaded: list[UnifiedToolManifest] = []
        for manifest in base:
            security_level, external_effects = self._action_safety_meta(
                manifest.name, actions
            )
            unified = UnifiedToolManifest(
                name=manifest.name,
                description=manifest.description,
                parameters=manifest.parameters,
                async_ok=manifest.async_ok,
                source=SOURCE_INTERNAL,
                security_level=security_level,
                external_effects=external_effects,
            )
            self._tools[unified.name] = unified
            loaded.append(unified)

        log_info(f"[tool_registry] Loaded {len(loaded)} internal action tool(s).")
        return loaded

    @staticmethod
    def _action_safety_meta(
        action_name: str,
        actions: dict[str, Any] | None,
    ) -> tuple[str, list[str]]:
        """Best-effort extraction of security metadata from an action def."""
        if actions:
            defn = actions.get(action_name)
            if isinstance(defn, dict):
                level = str(defn.get("security_level", "low"))
                effects = list(defn.get("external_effects", []) or [])
                return level, effects
        return "low", []

    # -- external MCP tools ------------------------------------------------

    def add_mcp_tool(
        self,
        server_name: str,
        name: str,
        description: str,
        parameters: list[ToolParameter],
        security_level: str = "medium",
        external_effects: list[str] | None = None,
    ) -> UnifiedToolManifest:
        """Register a tool discovered from an external MCP server.

        The tool name is namespaced as ``mcp_<server>_<name>`` to avoid
        collisions with internal action names, while ``server_name`` and the
        ``source`` field preserve the origin for the executor.
        """
        tool_name = f"mcp_{server_name}_{name}"
        unified = UnifiedToolManifest(
            name=tool_name,
            description=description or f"MCP tool {name} from {server_name}",
            parameters=parameters,
            source=f"{SOURCE_MCP}:{server_name}",
            server_name=server_name,
            security_level=security_level,
            external_effects=external_effects or [f"mcp:{server_name}"],
        )
        self._tools[tool_name] = unified
        self._mcp_sources[tool_name] = server_name
        log_debug(
            f"[tool_registry] Registered MCP tool '{tool_name}' "
            f"from server '{server_name}'."
        )
        return unified

    def clear_mcp_tools(self, server_name: str | None = None) -> None:
        """Drop MCP-sourced tools, optionally only for one server."""
        to_remove = [
            name
            for name, tool in self._tools.items()
            if tool.is_external()
            and (server_name is None or tool.server_name == server_name)
        ]
        for name in to_remove:
            self._tools.pop(name, None)
            self._mcp_sources.pop(name, None)

    # -- queries -----------------------------------------------------------

    def all_tools(self) -> list[UnifiedToolManifest]:
        """Return every registered tool (internal + external)."""
        return list(self._tools.values())

    def get_tool(self, name: str) -> UnifiedToolManifest | None:
        """Look up a single tool by (possibly namespaced) name."""
        return self._tools.get(name)

    def internal_tools(self) -> list[UnifiedToolManifest]:
        """Return only the internal action tools."""
        return [t for t in self._tools.values() if not t.is_external()]

    def mcp_tools(self) -> list[UnifiedToolManifest]:
        """Return only the external MCP tools."""
        return [t for t in self._tools.values() if t.is_external()]

    def tool_names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def as_manifests(self) -> list[ToolManifest]:
        """Return plain ``ToolManifest`` objects (Live-compatible)."""
        return [t for t in self._tools.values()]


# Module-level singleton used by the orchestrator / agent loop.
tool_registry = ToolRegistry()
