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

import asyncio
from typing import Any, Dict, List, Optional

from core.logging_utils import log_debug, log_error, log_info
from core.config_manager import config_registry

# Lane constants
FAST = "fast"
AGENT = "agent"

# Strong references to detached agent turns so they are not garbage-collected
# mid-flight (see AGENTS.md RUF006 note). The agent loop runs OFF the message
# chain consumer lock, so a background task holding the only reference would
# otherwise be reaped by the event loop before it finishes.
_AGENT_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

# interface_paths that currently have a detached agent turn in flight. Streaming
# HTTP interfaces (e.g. the OpenAI/Ollama-compatible server) return ``None`` from
# the message chain when the Agent Lane is taken (work is detached), which would
# otherwise make them finalize the stream immediately with no text. They consult
# this set to know they must WAIT for ``_deliver_agent_reply`` to stream the
# real final text instead of closing the stream empty.
_INFLIGHT_AGENT_INTERFACE_PATHS: set[str] = set()


def has_inflight_agent_turn(interface_path: str | None) -> bool:
    """Return True if a detached agent turn is currently in flight for ``interface_path``.

    Streaming interfaces use this to decide whether a ``None`` message-chain
    result means "no reply coming" (finalize now) or "agent working, reply will
    be delivered asynchronously" (keep the stream open and wait).
    """
    if not interface_path:
        return False
    return str(interface_path) in _INFLIGHT_AGENT_INTERFACE_PATHS


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


def _extract_resume_task_id(actions: List[Any]) -> int | None:
    """Return the task id from a ``resume_agent_task`` action, if the model emitted one.

    The model — not any keyword/text matching — decides to resume a specific
    task by emitting a ``resume_agent_task`` action carrying the numeric id it
    saw referenced in the conversation. This lets a user continue a task created
    on a *different* interface (e.g. a Grillo task referenced from Telegram),
    which interface-based auto-resume cannot bridge. Returns the first valid
    positive id found, or ``None``.
    """
    for a in actions:
        if not isinstance(a, dict):
            continue
        t = a.get("type") or a.get("action")
        if t != "resume_agent_task":
            continue
        payload = a.get("payload")
        raw = payload.get("task_id") if isinstance(payload, dict) else a.get("task_id")
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            return tid
    return None


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
        log_info("[agent_router] Routing to Agent Lane (detached)")
        goal = _derive_goal(actions, context)

        # Mark this interface as having an in-flight agent turn BEFORE spawning
        # the detached task. The message chain returns ``None`` to the interface
        # the instant we return below, so a streaming interface could otherwise
        # observe ``None`` and finalize its stream empty before the detached
        # turn has even started. Registering here closes that race window; the
        # detached task clears it in its ``finally`` once delivery is done.
        inflight_interface_path = (
            context.get("interface_path") if isinstance(context, dict) else None
        )
        if inflight_interface_path:
            _INFLIGHT_AGENT_INTERFACE_PATHS.add(str(inflight_interface_path))

        # If the model chose to resume a specific existing task (by emitting a
        # ``resume_agent_task`` action with a numeric id), capture that id and
        # hand it to the detached turn so it continues THAT task — even one
        # created on a different interface — instead of the interface-scoped
        # auto-resume.
        explicit_resume_id = _extract_resume_task_id(actions)

        # CRITICAL: the agent loop must NOT run inline here. The message chain
        # invokes this router while holding the consumer lock, so awaiting a
        # multi-iteration agentic turn (LLM calls + tool work, up to minutes)
        # would freeze Synth: no other queued message — a user writing mid-turn,
        # radio speech — could be processed until the agent finished.
        #
        # Instead we spawn the turn as a detached background task and return
        # immediately, releasing the consumer slot. The agent works
        # concurrently; when a user writes while it runs, that message flows
        # through the queue and is answered normally. The turn's final reply is
        # delivered from inside the task via ``_deliver_agent_reply``.
        task = asyncio.create_task(
            _run_agent_turn_detached(
                goal, context, bot, message, explicit_resume_id=explicit_resume_id
            )
        )
        _AGENT_BACKGROUND_TASKS.add(task)
        task.add_done_callback(_AGENT_BACKGROUND_TASKS.discard)
        return {"lane": "agent", "status": "accepted", "detached": True}

    # Fast Lane: unchanged direct execution.
    log_info("[agent_router] Routing to Fast Lane")
    from core.action_parser import run_actions

    return await run_actions(actions, context, bot, message)


async def _run_agent_turn_detached(
    goal: str,
    context: Dict[str, Any],
    bot: Any,
    message: Any,
    *,
    explicit_resume_id: int | None = None,
) -> None:
    """Run one agentic turn off the message-chain consumer lock.

    This coroutine is scheduled as a detached task by :func:`route`. It owns the
    full turn lifecycle — running the bounded loop and delivering the final text
    back to the originating interface — without ever blocking the queue
    consumer. Failures are logged and swallowed so a background turn can never
    take down the message chain.
    """
    try:
        from core.agent_core import AgentLoopManager

        manager = AgentLoopManager()

        # Resume-in-place: if this same interface already owns a paused
        # (``pending``) agentic task, the incoming message is the user granting
        # another batch — RESUME that task instead of spawning a duplicate. This
        # is purely interface-based (no keyword/language detection): a chat that
        # has a parked task and receives a new message continues it. The user's
        # words are appended as an observation so the model sees what was said
        # (e.g. an approval, a correction, extra detail) as it continues.
        resume_task_id: int | None = None
        prior_observations: list[Dict[str, Any]] | None = None
        resume_engine: str | None = None
        resume_goal: str | None = None
        interface_path = (
            context.get("interface_path") if isinstance(context, dict) else None
        )

        # Precedence: an explicit resume-by-id (the model chose a specific task,
        # possibly on another interface) wins over interface-scoped auto-resume.
        resumable: Optional[Dict[str, Any]] = None
        if explicit_resume_id is not None:
            resumable = await manager.find_task_by_id(explicit_resume_id)
            if resumable:
                log_info(
                    f"[agent_router] Resuming explicitly-referenced task "
                    f"{explicit_resume_id} (cross-interface allowed)"
                )
            else:
                log_info(
                    f"[agent_router] Requested task {explicit_resume_id} is not "
                    "resumable (unknown or not pending); falling back to "
                    "interface-scoped auto-resume"
                )
        if resumable is None:
            resumable = await manager.find_resumable_task_for_interface(interface_path)
        if resumable:
            resume_task_id = resumable["task_id"]
            resume_goal = resumable["goal"]
            resume_engine = resumable["engine"]
            prior_observations = resumable["prior_observations"]
            if goal and goal.strip():
                prior_observations = list(prior_observations or [])
                prior_observations.append(
                    {
                        "iteration": None,
                        "role": "user",
                        "content": goal.strip(),
                    }
                )
            log_info(
                f"[agent_router] Resuming pending task {resume_task_id} for "
                f"interface {interface_path!r} instead of opening a new one"
            )

        result = await manager.run_agentic_turn(
            goal=resume_goal or goal,
            engine=resume_engine,
            context=context,
            original_message=message,
            task_id=resume_task_id,
            prior_observations=prior_observations,
        )
        # The agent loop runs detached from the Fast-Lane reply path, so the
        # user's interface receives nothing while it works and nothing when it
        # finishes. Deliver the loop's final text back to the originating
        # interface so the user actually sees the outcome instead of silence.
        await _deliver_agent_reply(result, context, bot, message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_error(f"[agent_router] Detached agent turn failed: {exc}")
    finally:
        # Clear the in-flight marker so a waiting streaming interface stops
        # blocking. Delivery (or its failure) has already happened above, so any
        # stream still open will be finalized by ``_deliver_agent_reply`` (real
        # text) or fall through to its own timeout/fallback with no text.
        interface_path = (
            context.get("interface_path") if isinstance(context, dict) else None
        )
        if interface_path:
            _INFLIGHT_AGENT_INTERFACE_PATHS.discard(str(interface_path))


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

    # When the turn was paused (iteration budget exhausted without an explicit
    # completion), the model has already authored a natural-language "I'm not
    # finished, shall I continue?" message into ``final_text`` (see
    # ``AgentLoopManager._compose_pause_message``), in the conversation's own
    # language/tone. The task is parked as ``pending`` and the same interface
    # can simply reply to resume it — no hardcoded string, no WebUI-only button
    # reference. We deliver ``final_text`` exactly like any other reply.
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
