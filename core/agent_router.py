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

from core.logging_utils import log_debug, log_error, log_info
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
        context: Optional message-chain context. The authoritative routing
            signal ``agent_needed`` is read from here — it is set pre-LLM by the
            ``recon_agent_intent`` recon plugin, which semantically judges the
            *user's request* (not the shape of Synth's proposed actions).

    Returns:
        ``FAST`` or ``AGENT``.
    """
    # Two independent gates must BOTH be on to ever leave the Fast Lane:
    #  * AGENTIC_ROUTING_ENABLED — the Fast/Agent router feature flag.
    #  * AGENT_ENABLED — the user-facing agent on/off toggle (WebUI + agent
    #    plugin). When the user switches the agent OFF, behaviour must fall back
    #    to the classic Fast Lane exactly like the ``develop`` branch, even if
    #    the routing flag is still set. Keeping these decoupled caused the agent
    #    to keep engaging while toggled off.
    if not config_registry.get_var("AGENTIC_ROUTING_ENABLED", False, value_type=bool):
        return FAST
    if not config_registry.get_var("AGENT_ENABLED", True, value_type=bool):
        log_debug("[agent_router] AGENT_ENABLED off -> FAST lane (classic behaviour)")
        return FAST

    # Authoritative, pre-LLM decision: the recon plugin evaluated the user's
    # request and flagged it as agentic work. This is deterministic and does not
    # depend on how many actions the main model happened to emit — which is what
    # previously caused a plain greeting (message + diary/emotion) to be
    # misrouted to the Agent lane via the removed ``len(types) > 1`` heuristic.
    if context and context.get("agent_needed"):
        log_debug("[agent_router] context agent_needed -> AGENT lane")
        return AGENT

    if not actions:
        return FAST

    types = _action_types(actions)

    # A batch made up entirely of plain outbound message actions is NOT agentic
    # work — it is just Synth talking (possibly on several interfaces at once).
    # Those synth actions (e.g. ``message_telegram_bot``) must be recognised and
    # delivered through the classic Fast Lane so they never get swept into the
    # agent tool loop, where they would be executed as "tools" and interfere
    # with the agent.
    if types and all(_is_pure_message(t) for t in types):
        log_debug("[agent_router] Message-only batch -> FAST lane")
        return FAST

    # Safety net: any batch containing a real tool call is agentic work, even if
    # the recon somehow missed it. This keeps tool actions out of the Fast Lane.
    if any(_is_tool_call(t) for t in types):
        log_debug("[agent_router] Batch contains a tool call -> AGENT lane")
        return AGENT

    # No agent_needed flag and no tool call: the request was not judged agentic,
    # so it stays on the classic Fast Lane regardless of how many non-tool
    # actions the model emitted.
    log_debug("[agent_router] No agentic signal -> FAST lane")
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
        result = await manager.run_agentic_turn(
            goal=goal,
            context=context,
            original_message=message,
        )

        # The agent loop runs detached from the Fast-Lane reply path, so the
        # user's interface receives nothing while it works and nothing when it
        # finishes. Deliver the loop's final text back to the originating
        # interface so the user actually sees the outcome instead of silence.
        await _deliver_agent_reply(result, context, bot, message)
        return result

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


async def _deliver_agent_reply(
    result: Dict[str, Any],
    context: Dict[str, Any],
    bot: Any,
    message: Any,
) -> None:
    """Send the agent loop's final text back to the originating interface.

    The Agent Lane runs detached from the Fast-Lane reply path, so the user
    otherwise sees nothing when the loop finishes. We deliver ``final_text`` as
    an ordinary outbound ``message`` on the originating interface — exactly like
    a normal Telegram/Discord/WebUI reply — using the standard action dispatch
    so routing, TTS and history all behave as usual.
    """
    if not isinstance(result, dict):
        return
    final_text = result.get("final_text")
    if not isinstance(final_text, str) or not final_text.strip():
        return

    interface_path = context.get("interface_path") if context else None
    if not interface_path:
        log_debug(
            "[agent_router] No interface_path in context; skipping agent reply delivery"
        )
        return

    from core.interface_path_utils import get_interface_from_path
    from core.action_parser import run_action

    interface_name = get_interface_from_path(str(interface_path))
    if not interface_name:
        log_debug(
            f"[agent_router] Could not derive interface from path '{interface_path}'; "
            "skipping agent reply delivery"
        )
        return

    # The message_* action schema requires an explicit `target`. The message
    # plugin ultimately re-derives target/thread_id from interface_path, but
    # the action validator runs first and rejects the action outright when
    # `target` is missing — which silently dropped the agent's final reply
    # (the client saw an empty response). Derive target (and thread_id) from
    # interface_path here so the delivery action passes validation, matching
    # exactly what the message plugin would compute.
    payload: Dict[str, Any] = {
        "text": final_text,
        "interface_path": interface_path,
    }
    path_parts = str(interface_path).split("/")
    if len(path_parts) >= 2:
        payload["target"] = path_parts[1]
        if len(path_parts) >= 3:
            thread_id = path_parts[2].strip()
            if thread_id:
                payload["thread_id"] = thread_id
    action = {
        "type": f"message_{interface_name}",
        "interface": interface_name,
        "payload": payload,
    }
    try:
        log_info(
            f"[agent_router] Delivering agent reply to {interface_name} "
            f"(path={interface_path})"
        )
        await run_action(action, context, bot, message)
    except Exception as exc:
        log_error(f"[agent_router] Failed to deliver agent reply: {exc}")
