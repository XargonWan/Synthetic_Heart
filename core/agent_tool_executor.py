"""Agent tool executor — runs tools discovered by the unified registry.

Given a tool call ``(name, arguments)`` produced by an agentic LLM, this
executor:

* resolves the tool in :class:`core.tool_registry.ToolRegistry`;
* for **internal** actions, dispatches through the existing action pipeline
  (``core.action_parser.run_action``) so it inherits validation + safety +
  audit unchanged;
* for **external MCP** tools, calls the MCP client bridge
  (``core.mcp_bridge.client``) and normalizes the result back into a string
  observation.

This is the single execution gate for the Agent Lane: every tool — whether a
native SyntH action or a remote MCP tool — funnels through here and therefore
through :func:`core.action_safety.is_action_allowed_for_execution`.
"""

from __future__ import annotations

import json
from typing import Any

from core.logging_utils import log_error, log_info


def _stringify_mcp_result(result: Any) -> str:
    """Flatten an MCP ``call_tool`` result into a text observation."""
    if result is None:
        return ""

    # Newer SDK wraps content in result.content; some return a coroutine-free
    # object with .content list of blocks ({type, text|data}).
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                elif "data" in block:
                    parts.append(json.dumps(block["data"], default=str))
            else:
                text = getattr(block, "text", None)
                data = getattr(block, "data", None)
                if text is not None:
                    parts.append(str(text))
                elif data is not None:
                    parts.append(json.dumps(data, default=str))
        return "\n".join(parts)

    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)

    return str(result)


class AgentToolExecutor:
    """Executes tool calls for the agent loop."""

    def __init__(self, registry: Any | None = None) -> None:
        from core.tool_registry import tool_registry

        self.registry = registry or tool_registry

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
        original_message: Any = None,
    ) -> dict[str, Any]:
        """Execute one tool call and return a normalized result dict.

        Returns::
            {
                "ok": bool,
                "tool": str,
                "source": str,            # "internal" | "mcp:<server>"
                "result": str,            # text observation for the model
                "error": str | None,
            }
        """
        tool = self.registry.get_tool(tool_name)
        if tool is None:
            return {
                "ok": False,
                "tool": tool_name,
                "source": "unknown",
                "result": "",
                "error": f"Unknown tool: {tool_name}",
            }

        arguments = arguments or {}

        if tool.is_external():
            return await self._execute_mcp(tool, arguments)
        return await self._execute_internal(tool, arguments, context, original_message)

    async def _execute_internal(
        self,
        tool: Any,
        arguments: dict[str, Any],
        context: dict[str, Any] | None,
        original_message: Any,
    ) -> dict[str, Any]:
        """Run an internal SyntH action through the standard dispatch path."""
        from core.action_parser import run_action

        action = {"type": tool.name, "payload": arguments}
        try:
            log_info(f"[agent_tool_executor] Executing internal tool '{tool.name}'")
            result = await run_action(
                action,
                context or {"from_cortex": True, "agent_tool": True},
                None,
                original_message,
            )
            return {
                "ok": True,
                "tool": tool.name,
                "source": tool.source,
                "result": self._stringify_internal_result(result),
                "error": None,
            }
        except Exception as exc:
            log_error(
                f"[agent_tool_executor] Internal tool '{tool.name}' failed: {exc}"
            )
            return {
                "ok": False,
                "tool": tool.name,
                "source": tool.source,
                "result": "",
                "error": str(exc),
            }

    async def _execute_mcp(
        self, tool: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Invoke an external MCP tool via the client bridge."""
        from core.mcp_bridge.client import mcp_client_bridge

        try:
            log_info(
                f"[agent_tool_executor] Executing MCP tool '{tool.name}' "
                f"(server={tool.server_name})"
            )
            result = await mcp_client_bridge.call_tool(tool.name, arguments)
            return {
                "ok": True,
                "tool": tool.name,
                "source": tool.source,
                "result": _stringify_mcp_result(result),
                "error": None,
            }
        except Exception as exc:
            log_error(f"[agent_tool_executor] MCP tool '{tool.name}' failed: {exc}")
            return {
                "ok": False,
                "tool": tool.name,
                "source": tool.source,
                "result": "",
                "error": str(exc),
            }

    @staticmethod
    def _stringify_internal_result(result: Any) -> str:
        """Turn an action-parser result into a text observation."""
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            # Surface a useful summary; avoid dumping huge structures.
            if "result" in result and isinstance(result["result"], str):
                return result["result"]
            try:
                text = json.dumps(result, default=str)
            except Exception:
                text = str(result)
            return text[:4000]
        return str(result)


# Module-level singleton.
agent_tool_executor = AgentToolExecutor()
