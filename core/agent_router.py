"""Deterministic router for the Agentic Runtime 2.0.

Given a batch of parsed actions (the standard SyntH ``{"actions": [...]}`` shape),
the router decides which *lane* the message chain should take:

* **Fast Lane** — a single, side-effect-free, already-validated action (e.g. a
  plain ``message`` / ``tts_speak``). The message chain executes it directly via
  the existing ``run_actions`` path, exactly as today. No agent loop involved.
* **Agent Lane** — anything that needs the bounded agent loop: a tool call
  (internal action with external effects, or an ``mcp_*`` tool), multiple
  actions, or an action flagged as multi-step. The message chain hands the goal
  to :class:`core.agent_core.AgentLoopManager.run_agentic_turn`, which re-injects
  tool results into the model until it stops emitting tool calls.

The router is **pure and deterministic** — it never calls the LLM and never
touches the network. It only inspects action structure + the unified tool
registry's safety metadata. This keeps the critical Fast Lane path unchanged
when no agentic intent is present.

Feature flag: ``AGENTIC_ROUTING_ENABLED`` (default ``False``). When disabled the
router always returns ``FAST`` so existing behaviour is preserved.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logging_utils import log_debug, log_info
from core.config_manager import config_registry

# Lane constants
FAST = "fast"
AGENT = "agent"


def _action_types(actions: List[Any]) -> List[str]:
    """Extract canonical action type strings from a parsed action list."""
    types: List[str] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        t = a.get("type") or a.get("action")
        if isinstance(t, str):
            types.append(t)
    return types


def _is_tool_call(action_type: str) -> bool:
    """True if the action is a tool call (MCP tool or internal action w/ effects)."""
    if action_type.startswith("mcp_"):
        return True

    # Consult the unified registry for external-effect / tool classification.
    try:
        from core.tool_registry import tool_registry

        tool = tool_registry.get_tool(action_type)
        if tool is not None:
            # External MCP tools are always tool calls.
            if tool.is_external():
                return True
            # Internal actions flagged with external effects are tool calls.
            if getattr(tool, "external_effects", False):
                return True
    except Exception:
        # If the registry is unavailable, fall back to a conservative heuristic:
        # only mcp_ prefixed names are treated as tool calls.
        pass
    return False


def _is_pure_message(action_type: str) -> bool:
    """A plain outbound message action (no tool semantics)."""
    return action_type in (
        "message",
        "send_message",
        "message_telegram_bot",
        "message_discord_bot",
        "message_synth_webui",
        "message_matrix_chat",
        "message_ollama_serve",
        "radio_speak",
        "tts_speak",
    )


def classify(actions: List[Any], *, context: Dict[str, Any] | None = None) -> str:
    """Classify a parsed action batch into FAST or AGENT lane.

    Args:
        actions: The list of action dicts from the LLM response.
        context: Optional message-chain context (unused today, reserved for
            future per-interface overrides).

    Returns:
        ``FAST`` or ``AGENT``.
    """
    if not config_registry.get_var("AGENTIC_ROUTING_ENABLED", False, value_type=bool):
        return FAST

    if not actions:
        return FAST

    types = _action_types(actions)

    # Multi-action batches go to the Agent Lane (coordinated execution).
    if len(types) > 1:
        log_debug("[agent_router] Multiple actions -> AGENT lane")
        return AGENT

    if len(types) == 1:
        t = types[0]
        if _is_tool_call(t):
            log_debug(f"[agent_router] Tool call '{t}' -> AGENT lane")
            return AGENT
        if _is_pure_message(t):
            log_debug(f"[agent_router] Pure message '{t}' -> FAST lane")
            return FAST

    # Unknown single action: keep it on the Fast Lane (unchanged behaviour).
    log_debug("[agent_router] Unrecognized single action -> FAST lane")
    return FAST


async def route(
    actions: List[Any],
    *,
    context: Dict[str, Any],
    bot: Any = None,
    message: Any = None,
) -> Dict[str, Any]:
    """Route a parsed action batch to the appropriate lane and execute it.

    Returns the result dict from whichever lane handled the batch.
    """
    lane = classify(actions, context=context)
    if lane == AGENT:
        log_info("[agent_router] Routing to Agent Lane")
        from core.agent_core import AgentLoopManager

        goal = _derive_goal(actions, context)
        manager = AgentLoopManager()
        return await manager.run_agentic_turn(
            goal=goal,
            context=context,
            original_message=message,
        )

    # Fast Lane: unchanged direct execution.
    log_info("[agent_router] Routing to Fast Lane")
    from core.action_parser import run_actions

    return await run_actions(actions, context, bot, message)


def _derive_goal(actions: List[Any], context: Dict[str, Any] | None) -> str:
    """Best-effort goal string for the agent loop from the parsed actions."""
    if context:
        for key in ("goal", "original_text", "original_user_message", "user_text"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # Fall back to a compact JSON description of the requested actions.
    import json

    try:
        return f"Execute: {json.dumps(actions, default=str)}"
    except Exception:
        return "Execute the requested agentic actions."
