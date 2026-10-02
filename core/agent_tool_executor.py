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
        # The agent-tool marker is what tells a SELF-DELIVERING action (e.g.
        # ``search_current_knowledge``) that it is being called as a pure tool
        # inside the bounded loop: return the results to the loop and do NOT
        # enqueue a separate user-facing delivery turn. It is set HERE, on the
        # single choke point every agent tool call passes through, because the
        # callers always hand over a populated turn context — a
        # ``context or {default}`` expression never applies, so the marker was
        # absent in practice and the guard that reads it could never fire
        # (observed live: every agent-lane web search also sent the user a
        # message). Marked in place so any key a downstream handler writes back
        # onto the context stays visible to the caller.
        if isinstance(context, dict):
            context.setdefault("from_cortex", True)
            context["agent_tool"] = True
        else:
            context = {"from_cortex": True, "agent_tool": True}
        try:
            log_info(f"[agent_tool_executor] Executing internal tool '{tool.name}'")
            result = await run_action(action, context, None, original_message)
            # run_action returns {"ok": bool, ...} for message-delivery actions
            # dispatched through interfaces; every other action returns its
            # plugin's own result dict, which reports failure through its own
            # vocabulary ({"status": "error", ...}). Both are read here so the
            # loop's outcome is real instead of always claiming success.
            ok, error = self._outcome(result)
            return {
                "ok": ok,
                "tool": tool.name,
                "source": tool.source,
                "result": self._stringify_internal_result(result),
                "error": error,
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
    def _outcome(result: Any) -> tuple[bool, str | None]:
        """Read the success/failure outcome of one dispatched internal action.

        An explicit ``{"ok": bool}`` (returned by the message-delivery path) is
        authoritative. Otherwise the action returned its plugin's own result
        dict, which reports failure in its own vocabulary — ``{"status":
        "error", "message": ...}`` or a non-empty ``error`` — and that must NOT
        be reported as success: the loop renders this outcome to the model as
        ``[tool:x] OK`` / ``[tool:x] ERROR: ...``, so treating "did not raise"
        as success silently tells the model a failed tool worked (observed
        live: ``hass_snapshot`` answering ``status: error`` on a camera that
        returned no image, logged as ``ok=True``, after which the loop kept
        re-issuing it and then went off to search the web instead). Anything
        else counts as success.
        """
        if not isinstance(result, dict):
            return True, None
        if "ok" in result:
            return bool(result["ok"]), result.get("error")
        status = str(result.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            detail = (
                result.get("message") or result.get("error") or result.get("reason")
            )
            return False, str(detail) if detail else None
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return False, error.strip()
        return True, None

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
