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

The router is gated by the single authoritative agent toggle ``AGENT_ENABLED``
(the user-facing on/off switch). When the agent is disabled the router always
returns ``FAST`` so existing behaviour is preserved.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.logging_utils import log_debug, log_error, log_info, log_warning
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
        "message_discord_bot",
        "message_fluxer_bot",
        "message_integration",
        "message_mate_engine",
        "message_matrix_chat",
        "message_ollama_serve",
        "message_synth_webui",
        "message_telegram_bot",
        "radio_speak",
        "tts_speak",
    )


def _is_vessel_embodiment_turn(context: Dict[str, Any] | None) -> bool:
    """True when the current turn originates from a Rift Vessel embodiment.

    A Vessel turn (SyntH acting "in the world") must NEVER leave the Fast Lane:
    per AGENTS.md §5c the embodiment verbs declare no ``external_effects`` and
    must stay on the classic ``run_actions`` path — they must never spawn an
    agentic task / Drone. Thin wrapper over the single canonical structural
    detector :func:`core.interface_path_utils.is_vessel_embodiment_context`
    (routing metadata only, never message text — project rule: no keyword
    logic), which mirrors ``core.history_engine.build_context``. Fully guarded:
    any failure degrades to ``False`` so the normal routing path is untouched.
    """
    try:
        from core.interface_path_utils import is_vessel_embodiment_context

        return is_vessel_embodiment_context(context)
    except Exception:
        return False


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
    # Single authoritative gate: the user-facing agent on/off toggle (WebUI +
    # agent plugin). If the agent is enabled, the router is active; if the user
    # switches the agent OFF, behaviour falls back to the classic Fast Lane
    # exactly like the ``develop`` branch. (The old AGENTIC_ROUTING_ENABLED
    # feature flag was removed — a second layer that silently kept the Agent
    # Lane off even when the agent was enabled.)
    if not config_registry.get_var("AGENT_ENABLED", True, value_type=bool):
        log_debug("[agent_router] AGENT_ENABLED off -> FAST lane (classic behaviour)")
        return FAST

    # Rift Vessel embodiment turns must ALWAYS stay on the Fast Lane (AGENTS.md
    # §5c): the ``vessel_*`` verbs carry no external effects and must be executed
    # directly by ``run_actions``, never handed to the agent loop / Drones. This
    # gate wins over ``agent_needed`` because the recon plugin judges the *user's
    # request* and can flag an embodiment "moment of will" beat as agentic work,
    # which would misroute the turn to the Agent Lane and leave the vessel
    # actions unexecuted (0 processed).
    if _is_vessel_embodiment_turn(context):
        log_info("[agent_router] classify: vessel embodiment turn -> FAST lane")
        return FAST

    # G.R.I.L.L.O. (and other autonomous beat) turns must NEVER enter the Agent
    # Lane. A Grillo reflection beat is not a user request — its prompt already
    # drives its own multi-step actions (get_recent_chats / get_emotion_state /
    # diary), and wrapping it in the agentic loop turns one beat into a
    # 30-iteration tool-calling turn with the attempt_completion contract
    # (Langfuse 5fe657db). Structural signal only (the beat's interface, never
    # message text); the beat's actions stay on the Fast Lane via run_actions.
    if isinstance(context, dict):
        _beat_interface = str(
            context.get("interface") or context.get("interface_path") or ""
        )
        if _beat_interface == "grillo" or _beat_interface.startswith("grillo/"):
            log_info("[agent_router] classify: grillo beat -> FAST lane")
            return FAST

    # Authoritative, pre-LLM decision: the recon plugin evaluated the user's
    # request and flagged it as agentic work. It escalates even when the main
    # model only emitted conversational actions — the under-emission case where
    # the model promised work instead of doing it (Langfuse 0ee26438: "read
    # this pdf into a voice note" was answered with "I'll have the voice note
    # sent over in a moment!" and no tool call). The Agent Lane re-runs from
    # the user's actual request text, and its loop handles over-routing safely:
    # a message-only first iteration is delivered once and the turn ends
    # (``no_tools_required``), tool-call text-protocol output is parsed, and
    # delivered messages are never re-sent (deduped from final_text). The
    # earlier misrouted-roleplay regression (Langfuse 86141208) is handled
    # downstream in goal derivation and the loop, not by this guard.
    if context and context.get("agent_needed"):
        log_info("[agent_router] classify: context agent_needed -> AGENT lane")
        return AGENT

    if not actions:
        return FAST

    types = _action_types(actions)

    # Safety net: any batch containing a real tool call is agentic work, even if
    # the recon somehow missed it. This keeps tool actions out of the Fast Lane.
    if any(_is_tool_call(t) for t in types):
        log_info("[agent_router] classify: batch contains a tool call -> AGENT lane")
        return AGENT

    # No agent_needed flag and no tool call: the request was not judged
    # agentic, so it stays on the classic Fast Lane regardless of how many
    # non-tool actions the model emitted (the plain-greeting regression from
    # the removed ``len(types) > 1`` heuristic does not return).
    log_info("[agent_router] classify: no agentic signal -> FAST lane")
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

        # Under-emission seeding: when the main model produced no tool call,
        # execute its bookkeeping actions (emotion/diary/etc.) before the loop
        # starts so the persona's turn still lands; the loop then works the
        # real goal (Langfuse 0ee26438).
        seed_calls = _seed_calls_for_under_emission(actions)

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
                goal,
                # Snapshot the context for the detached task. ``route`` returns
                # immediately and the message chain keeps mutating the shared
                # ctx dict afterwards; the detached loop must see a stable copy
                # so per-turn routing state (attachment_paths, interface, ...)
                # can never be mutated out from under it mid-flight.
                dict(context) if isinstance(context, dict) else context,
                bot,
                message,
                explicit_resume_id=explicit_resume_id,
                preplanned_calls=seed_calls or None,
                preplanned_then_loop=bool(seed_calls),
            )
        )
        _AGENT_BACKGROUND_TASKS.add(task)
        task.add_done_callback(_AGENT_BACKGROUND_TASKS.discard)
        return {"lane": "agent", "status": "accepted", "detached": True}

    # Fast Lane: unchanged direct execution.
    log_info("[agent_router] Routing to Fast Lane")
    from core.action_parser import run_actions

    return await run_actions(actions, context, bot, message)


def start_task_resume(
    task_id: int,
    *,
    context: Dict[str, Any],
    bot: Any = None,
    message: Any = None,
) -> bool:
    """Kick off a detached resume of a specific ``pending`` agent task.

    This is the manual counterpart to the model-driven ``resume_agent_task``
    action: it lets an interface command (e.g. ``/task resume <id>``) continue a
    parked task on demand. The turn runs OFF the message-chain consumer lock via
    the same detached machinery as :func:`route`, so the caller returns
    immediately and the final reply is delivered asynchronously through
    :func:`_deliver_agent_reply`.

    The interface is registered as in-flight BEFORE the task is spawned so a
    streaming interface (OpenAI/Ollama-compatible) waits for the async delivery
    instead of finalizing its stream empty (same race guard as ``route``).

    Returns ``True`` if the resume task was scheduled, ``False`` on invalid input.
    """
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return False
    if tid <= 0:
        return False

    inflight_interface_path = (
        context.get("interface_path") if isinstance(context, dict) else None
    )
    if inflight_interface_path:
        _INFLIGHT_AGENT_INTERFACE_PATHS.add(str(inflight_interface_path))

    task = asyncio.create_task(
        _run_agent_turn_detached("", context, bot, message, explicit_resume_id=tid)
    )
    _AGENT_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_AGENT_BACKGROUND_TASKS.discard)
    log_info(f"[agent_router] Manual resume scheduled for task {tid}")
    return True


async def _run_agent_turn_detached(
    goal: str,
    context: Dict[str, Any],
    bot: Any,
    message: Any,
    *,
    explicit_resume_id: int | None = None,
    preplanned_calls: list[Dict[str, Any]] | None = None,
    preplanned_then_loop: bool = False,
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
                allowed, reason = _resume_allowed(
                    str(resumable.get("goal") or ""), context
                )
                if not allowed:
                    if reason != "poisoned_goal":
                        log_warning(
                            f"[agent_router] Refusing auto-resume of task "
                            f"{resumable['task_id']}: {reason}"
                        )
                        resumable = None
                    # "poisoned_goal" falls through to the unified gate below,
                    # which cancels the row and clears it for both resume paths.
                elif not _resume_age_allowed(
                    resumable.get("updated_at"),
                    int(config_registry.get_var("AGENT_RESUME_MAX_AGE_SEC", 900)),
                ):
                    # Stale parked task: the user's "keep going" reply arrives
                    # within minutes of the pause — an old pending row is
                    # abandoned work and must not hijack a later conversational
                    # turn (Langfuse 00:49 chain). Start fresh instead; the
                    # stale row stays pending for explicit WebUI resume.
                    log_warning(
                        f"[agent_router] Refusing auto-resume of task "
                        f"{resumable['task_id']}: parked too long ago "
                        f"(AGENT_RESUME_MAX_AGE_SEC)"
                    )
                    resumable = None
        # Poisoned-goal gate — applies to BOTH resume paths (explicit WebUI
        # resume / resume_agent_task action AND interface auto-resume). A
        # parked task whose stored goal is the model's own self-referential
        # LLM JSON artifact (pre-fix data, Langfuse 7b31c7c8 / task 198) can
        # never be meaningfully resumed: refuse it and cancel the row
        # best-effort so it stops being offered in the WebUI and cannot
        # hijack future turns.
        if resumable is not None:
            stored_goal = str(resumable.get("goal") or "")
            if _looks_like_llm_response_json(stored_goal):
                log_warning(
                    f"[agent_router] Refusing resume of task "
                    f"{resumable['task_id']}: poisoned goal artifact"
                )
                await manager.supersede_pending_task(resumable["task_id"])
                resumable = None
        if resumable:
            resume_task_id = resumable["task_id"]
            resume_goal = resumable["goal"]
            resume_engine = resumable["engine"]
            # Bound the re-injected history: a parked task may have been
            # resumed repeatedly, accumulating dozens of stale observations
            # that bloat every iteration prompt. Keep the most recent 20.
            prior_observations = list((resumable.get("prior_observations") or [])[-20:])
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
            preplanned_calls=preplanned_calls,
            preplanned_then_loop=preplanned_then_loop,
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


def _looks_like_llm_response_json(text: str) -> bool:
    """True when ``text`` is a JSON object shaped like an LLM action response.

    The message chain stores the model's RAW response text into
    ``context["goal"]`` / ``context["original_text"]`` on LLM-origin turns
    (``core/message_chain.py``), so those keys can hold the model's own action
    batch rather than the user's request. A goal that IS the model's own
    output is self-referential garbage (Langfuse 86141208: the agent loop was
    asked to "achieve" the reply Synth had already written). This structural
    check (never keyword logic) identifies such values so the goal derivation
    can skip them. Conservative: only values that actually parse as a JSON
    actions/tool-call object are rejected.
    """
    stripped = text.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        parsed = json.loads(stripped)
    except Exception:
        return False
    if isinstance(parsed, dict):
        if any(k in parsed for k in ("actions", "tool_calls", "calls")):
            return True
        if any(k in parsed for k in ("type", "name", "tool")) and any(
            k in parsed for k in ("payload", "arguments", "params", "args")
        ):
            return True
    return False


def _seed_calls_for_under_emission(actions: List[Any]) -> list[Dict[str, Any]]:
    """Extract the main model's bookkeeping actions for pre-loop seeding.

    When the recon flag escalates a turn whose batch contains NO real tool
    call (the under-emission case — the model promised work instead of doing
    it, Langfuse 0ee26438), the loop discards the main model's actions and
    re-runs from the user's request. Seeding the non-message actions
    (update_emotion_state, diary, etc.) lets that bookkeeping still land
    before the loop starts.

    Message actions are NEVER seeded: the loop's model owns conversation, and
    delivering the promise text twice would duplicate a user-facing message.
    Tool calls are NEVER seeded: they would execute twice (once here, once by
    the loop) with duplicated side effects. Returns an empty list when there
    is nothing safe to seed (or a tool call is present).
    """
    types = _action_types(actions)
    if not actions or not types:
        return []
    if any(_is_tool_call(t) for t in types):
        return []
    seeds: list[Dict[str, Any]] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        name = a.get("type") or a.get("action")
        if not isinstance(name, str) or not name.strip():
            continue
        if _is_pure_message(name):
            continue
        payload = a.get("payload")
        seeds.append(
            {
                "name": name.strip(),
                "arguments": payload if isinstance(payload, dict) else {},
            }
        )
    return seeds


def _resume_allowed(
    stored_goal: str, context: Dict[str, Any] | None
) -> tuple[bool, str]:
    """Structural gate for interface-scoped auto-resume of a parked task.

    A parked (``pending``) task is resumed when the next message from the same
    interface is a continuation ("yes, keep going"). Two fresh-request signals
    must NOT auto-resume (Langfuse 7b31c7c8 chain): the stored goal being the
    model's own self-referential JSON artifact (pre-fix poisoned rows that can
    never be meaningfully continued), or the incoming turn carrying new
    uploaded attachments (fresh material = a new task). Purely structural —
    never message-text/keyword logic.

    The poisoned-goal refusal is enforced by the caller for BOTH resume paths
    (explicit WebUI resume / ``resume_agent_task`` action AND interface
    auto-resume); the helper still reports it so the auto path can distinguish
    reasons.

    Returns ``(allowed, reason)``; ``reason`` is a short machine label when
    refused (``"poisoned_goal"`` / ``"fresh_attachments"``), else ``""``.
    """
    if _looks_like_llm_response_json(stored_goal):
        return False, "poisoned_goal"
    if isinstance(context, dict):
        attachment_paths = context.get("attachment_paths")
        if isinstance(attachment_paths, (list, tuple)) and attachment_paths:
            return False, "fresh_attachments"
    return True, ""


def _resume_age_allowed(
    updated_at: Any, max_age_sec: int, *, now: datetime | None = None
) -> bool:
    """Structural freshness gate for interface-scoped auto-resume.

    A parked (``pending``) task may only be auto-resumed when it was parked
    recently: the user's "keep going" reply arrives within seconds or minutes
    of the pause message. An old pending row is abandoned work and must never
    hijack a later conversational turn (Langfuse 00:49 chain: a 3.5h-old task
    absorbed a plain chat message). Purely structural (timestamps), never
    message text. A missing/unparseable timestamp degrades to allowed=True so
    the pre-existing behavior is preserved.
    """
    if updated_at is None:
        return True
    try:
        reference = now or datetime.now(timezone.utc)
        age_seconds = (reference - updated_at).total_seconds()
    except Exception:
        return True
    return age_seconds <= float(max_age_sec)


def _derive_goal(actions: List[Any], context: Dict[str, Any] | None) -> str:
    """Best-effort goal string for the agent loop from the parsed actions.

    The goal must be the USER's request, never the model's own output: on
    LLM-origin turns ``context["goal"]`` / ``context["original_text"]`` hold
    the raw LLM response (message_chain stores it there), so reading those
    keys first produced self-referential goals — the agent loop was asked to
    "achieve" the reply Synth had already written (Langfuse 86141208).
    """
    if context:
        # Preferred: the user's actual request text (set from the inbound
        # message by plugin_instance / message_chain — never the model reply).
        for key in ("original_user_message", "user_text"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Secondary: goal/original_text, but only when they are NOT the
        # model's own JSON response.
        for key in ("goal", "original_text"):
            value = context.get(key)
            if not (isinstance(value, str) and value.strip()):
                continue
            candidate = value.strip()
            if _looks_like_llm_response_json(candidate):
                continue
            return candidate
    # Fallback: describe the model's own planned actions, but ONLY when they
    # are genuine tool work — a message-only batch reaching this point means
    # the caller misrouted a conversational reply and the loop must not re-run
    # it (the router keeps such batches on the Fast Lane).
    if actions and any(_is_tool_call(t) for t in _action_types(actions)):
        try:
            return f"Execute: {json.dumps(actions, default=str)}"
        except Exception:
            pass
    return "Complete the user's request using the available tools."


def _agent_actions_executed(result: Dict[str, Any]) -> int:
    """Count executed tool actions from the loop's observations.

    Mirrors the persistence path in ``agent_core.py``: every ``tool_results``
    observation carries the executed tool results in its ``content`` list, so
    ``0`` means the model produced no usable tool work this turn.
    """
    count = 0
    for obs in result.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        if obs.get("role") == "tool_results":
            content = obs.get("content")
            if isinstance(content, list):
                count += len(content)
    return count


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

    # Garbage-output guard. When the turn times out with ZERO tool actions
    # executed, the loop's ``final_text`` is raw model text that may be a
    # degenerate artifact (e.g. ``"thought\nthought"`` after an empty-body burst
    # from the endpoint). Retrying is the loop's job (primary re-call +
    # Base-Cortex safety net, plus the bridge's ``retry_on_empty``) — this only
    # stops the artifact from being shipped to the user as a reply. Purely
    # structural (stop_reason + executed-action count), never keyword logic; the
    # pause path (``paused_max_iterations``) composes its own message and is
    # untouched.
    stop_reason = str(result.get("stop_reason") or "").strip()
    if stop_reason == "timeout" and _agent_actions_executed(result) == 0:
        log_warning(
            "[agent_router] Suppressing agent reply delivery: turn timed out "
            f"with 0 actions executed (final_text={final_text!r})"
        )
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
        delivery_result = await run_action(action, context, bot, message)

        # Check delivery success — run_action now returns {"ok": bool} for
        # message-delivery actions dispatched through interfaces.
        ok = isinstance(delivery_result, dict) and delivery_result.get("ok") is True
        if not ok:
            error_detail = (
                delivery_result.get("error", "unknown")
                if isinstance(delivery_result, dict)
                else str(delivery_result)
            )
            log_error(
                f"[agent_router] Agent reply delivery failed "
                f"(interface={interface_name}, path={interface_path}): "
                f"{error_detail}. Retrying once after 2 s delay."
            )
            await asyncio.sleep(2)
            retry_result = await run_action(action, context, bot, message)
            retry_ok = isinstance(retry_result, dict) and retry_result.get("ok") is True
            if retry_ok:
                log_info("[agent_router] Agent reply delivery succeeded on retry")
            else:
                retry_error = (
                    retry_result.get("error", "unknown")
                    if isinstance(retry_result, dict)
                    else str(retry_result)
                )
                log_error(
                    f"[agent_router] Agent reply delivery FAILED after retry "
                    f"(interface={interface_name}, path={interface_path}): "
                    f"{retry_error}. Message lost."
                )
    except Exception as exc:
        log_error(f"[agent_router] Failed to deliver agent reply: {exc}")
