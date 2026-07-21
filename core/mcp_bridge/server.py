"""Expose selected SyntH actions as an MCP server (FastMCP).

This lets external MCP *clients* (e.g. coding agents, other AIs) drive Synth's
native actions over the Model Context Protocol. It is the inverse of the client
bridge (``core.mcp_bridge.client``): there Synth *consumes* remote MCP tools;
here Synth *publishes* its own actions as MCP tools.

Only actions explicitly whitelisted via the ``AGENT_MCP_EXPOSED_ACTIONS`` config
(or, when unset, a safe default subset) are exposed, and every invocation still
passes through :func:`core.action_safety.is_action_allowed_for_execution` so the
existing policy/approval gates apply unchanged.

This module is intentionally isolated from the dev MCP tooling (``.mcp.json``,
``mcp_servers/*.py``) — it is part of Synth's own runtime MCP support.
"""

from __future__ import annotations

import json
from typing import Any

from core.logging_utils import log_info, log_warning

# Default safe subset of actions exposed when no explicit whitelist is set.
DEFAULT_EXPOSED_ACTIONS = (
    "tts_speak",
    "message_synth_webui",
    "create_personal_diary_entry",
)


def _get_exposed_action_names() -> list[str]:
    """Resolve the whitelist of action names to expose as MCP tools."""
    from core.config_manager import config_registry

    raw = config_registry.get_var("AGENT_MCP_EXPOSED_ACTIONS", "", value_type=str)
    if raw and isinstance(raw, str) and raw.strip():
        return [a.strip() for a in raw.split(",") if a.strip()]
    return list(DEFAULT_EXPOSED_ACTIONS)


def build_server(name: str = "synth-actions") -> Any:
    """Build (but do not run) a FastMCP server exposing selected SyntH actions.

    Returns the FastMCP instance. Call ``.run()`` / ``.run_async()`` on it from
    the deployment layer, or mount it via the FastMCP ASGI app.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - environment dependent
        log_warning(f"[mcp_bridge.server] FastMCP unavailable: {exc}")
        raise

    mcp = FastMCP(name)

    # Capture the action registry at build time.
    exposed = _get_exposed_action_names()

    # Pull action schemas from the unified tool registry so the MCP tool
    # signatures match the real action parameters.
    try:
        from core.tool_registry import tool_registry

        registry = tool_registry
    except Exception:
        registry = None

    for action_name in exposed:
        tool = registry.get_tool(action_name) if registry else None
        params_schema: dict[str, Any] = {"type": "object", "properties": {}}
        if tool is not None and getattr(tool, "parameters", None):
            params_schema = _tool_parameters_to_schema(tool.parameters)

        _register_action_tool(mcp, action_name, params_schema)

    log_info(f"[mcp_bridge.server] Built MCP server '{name}' exposing: {exposed}")
    return mcp


def _tool_parameters_to_schema(parameters: Any) -> dict[str, Any]:
    """Convert a list[ToolParameter] into a JSON-schema object for FastMCP."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in parameters or []:
        prop: dict[str, Any] = {"type": getattr(p, "type", "string") or "string"}
        if getattr(p, "description", None):
            prop["description"] = p.description
        if getattr(p, "enum", None):
            prop["enum"] = p.enum
        properties[p.name] = prop
        if getattr(p, "required", False):
            required.append(p.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _register_action_tool(
    mcp: Any, action_name: str, params_schema: dict[str, Any]
) -> None:
    """Register a single SyntH action as an MCP tool on the FastMCP server."""

    # Build a dynamic async handler that dispatches through run_action.
    async def _handler(**kwargs: Any) -> str:
        from core.action_parser import run_action
        from core.action_safety import is_action_allowed_for_execution

        allowed, reason, _meta = is_action_allowed_for_execution(
            {"type": action_name, "payload": kwargs},
            {"from_cortex": True, "agent_tool": True, "mcp_exposed": True},
            None,
        )
        if not allowed:
            return f"Action blocked by policy: {reason}"

        result = await run_action(
            {"type": action_name, "payload": kwargs},
            {"from_cortex": True, "agent_tool": True, "mcp_exposed": True},
            None,
            None,
        )
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            if "result" in result and isinstance(result["result"], str):
                return result["result"]
            return json.dumps(result, default=str)
        return str(result)

    # Attach a docstring + input schema so clients see a real tool.
    _handler.__name__ = f"synth_{action_name}"
    _handler.__doc__ = f"Execute the SyntH action '{action_name}'."
    try:
        mcp.add_tool(
            _handler, name=f"synth_{action_name}", description=_handler.__doc__
        )
    except TypeError:
        # Older FastMCP signatures accept inputSchema via decorator.
        mcp.tool(name=f"synth_{action_name}")(_handler)


# Module-level lazy singleton (built on first access).
_mcp_server_instance: Any | None = None


def get_mcp_server(name: str = "synth-actions") -> Any:
    """Return the (lazily built) shared FastMCP server instance."""
    global _mcp_server_instance
    if _mcp_server_instance is None:
        _mcp_server_instance = build_server(name)
    return _mcp_server_instance
